import sys, os, logging
from datetime import datetime
import pickle, torch

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
print(BASE_DIR)
sys.path.insert(1, BASE_DIR)

from config import device
from plants import SystemRobots
from controllers import RENController
from loss_functions import RobotsLoss
from assistive_functions import WrapLogger
# from experiments.robotsX.detect_collision import *
# from experiments.robotsX.plots import plot_trajectories


random_seed = 5
torch.manual_seed(random_seed)

# ------ EXPERIMENT ------
col_av = True
obstacle = True
is_linear = False

# ----- SET UP LOGGER -----
now = datetime.now().strftime("%m_%d_%H_%M_%S")
save_path = os.path.join(BASE_DIR, 'experiments', 'minimal_example', 'saved_results')
save_folder = os.path.join(save_path, 'ren_controller_'+now)
os.makedirs(save_folder)
# logger
logging.basicConfig(filename=os.path.join(save_folder, 'log'), format='%(asctime)s %(message)s', filemode='w')
logger = logging.getLogger('ren_controller_')
logger.setLevel(logging.DEBUG)
logger = WrapLogger(logger)

# ------------ 1. Dataset ------------
t_end = 100
std_ini = 0.2
n_agents = 2
num_rollouts = 30

filename = 'data_T'+str(t_end)+'_stdini'+str(std_ini)+'_agents'+str(n_agents)+'_RS'+str(random_seed)+'.pkl'
filename = os.path.join(save_path, filename)
if not os.path.isfile(filename):
    logger.info("[ERR] " + filename + " does not exists.")
    logger.info("Need to generate data!")
assert os.path.isfile(filename)
filehandler = open(filename, 'rb')
data_saved = pickle.load(filehandler)
filehandler.close()
x0 = data_saved['x0'].to(device)
xbar = data_saved['xbar'].to(device)

train_data = data_saved['train_data_full'][:num_rollouts, :, :].to(device)
assert train_data.shape[0] == num_rollouts
test_data = data_saved['test_data'].to(device)
# data for plot
t_ext = t_end * 4
plot_data = torch.zeros(t_ext, train_data.shape[-1])
plot_data[0, :] = (x0.detach() - xbar)
plot_data = plot_data.to(device)
msg = '\n[INFO] Dataset: n_agents: %i' % n_agents + ' -- num_rollouts: %i' % num_rollouts
msg += ' -- std_ini: %.2f' % std_ini + ' -- time horizon: %i' % t_end
msg += '\n[INFO] Initial condition: x_0: ' + str(x0) + ' -- xbar: ' + str(xbar)
logger.info(msg)

# ------------ 2. Plant ------------
k = 1.0         # spring constant
plant_input_init = None     # all zero
plant_state_init = None     # same as xbar
sys = SystemRobots(
    xbar=xbar, x_init=plant_state_init,
    u_init=plant_input_init, is_linear=is_linear, k=k
)
msg = '\n[INFO] Plant: spring k: %.2f' % k + ' -- use linearized plant: ' + str(is_linear)
logger.info(msg)

# ------------ 3. Controller ------------
initialization_std = 0.1
dim_internal = 8    # size of the linear part of REN
l = 8               # size of the non-linear part of REN
ctl = RENController(
    noiseless_forward=sys.noiseless_forward,
    input_init=sys.x_init, output_init=sys.u_init,
    dim_internal=dim_internal, l=l,
    initialization_std=initialization_std,
    output_amplification=20,
)
msg = '\n[INFO] Controller: dimension of the internal state: %i' % dim_internal
msg += ' -- l: %i' % l + ' -- initialization_std: %.2f'% initialization_std
logger.info(msg)

# ------------ 4. Loss ------------
Q = torch.kron(torch.eye(n_agents), torch.eye(4)).to(device)
alpha_u = 0.1/400
alpha_ca = 100 if col_av else None
alpha_obst = 5e3 if obstacle else None
min_dist = 1.
loss_fn = RobotsLoss(
    Q=Q, alpha_u=alpha_u, xbar=xbar,
    loss_bound=None, sat_bound=None,
    alpha_ca=alpha_ca, alpha_obst=alpha_obst,
    min_dist=min_dist if col_av else None,
    n_agents=sys.n_agents if col_av else None,
)
msg = '\n[INFO] Loss:  alpha_u: %.6f' % alpha_u
msg += ' -- alpha_ca: %.f' % alpha_ca if col_av else ' -- no collision avoidance'
msg += ' -- alpha_obst: %.1f' % alpha_obst if obstacle else ' -- no obstacle avoidance'
logger.info(msg)

# ------------ 5. Optimizer ------------
batch_size = 5
epochs = 5000 if col_av else 100
learning_rate = 2e-3 if col_av else 5e-3
early_stopping = False       # return the best model on the validation data among all validated iteration
valid_data = train_data      # use the entire train data for validation
valid_period = 5000          # validate after every 'valid_period' iterations
assert not (valid_data is None and early_stopping)
optimizer = torch.optim.Adam(ctl.parameters(), lr=learning_rate)
msg = '\n[INFO] Solver: lr: %.2e' % learning_rate + ' -- epochs: %i' % epochs
msg += ' -- batch_size: %i' % batch_size + ', -- early stopping:' + str(early_stopping)
logger.info(msg)

# ------------ 3. Before training ------------

# msg = '------------- ' + exp_name + ' EXPERIMENT - EMPIRICAL -------------'
x_log, _, u_log = sys.rollout(ctl, plot_data)
# # Plots:
# fname_plot = 'CL'
# filename = os.path.join(BASE_DIR, 'experiments', 'robotsX', 'saved_results', fname_plot)
# plot_trajectories(
#     x_log[0, :, :], # remove extra dim due to batching
#     xbar, sys.n_agents, exp_name=exp_name, filename=filename, text="CL - before training", T=t_ext
# )
# # collisions before training
# num_col = detect_collisions_singletraj(
#     x_log[0, :, :], # remove extra dim due to batching
#     n_agents, min_dist)
# msg += '\nBefore training: Number of collisions in train data = ' + str(num_col)

# ------------ 4. Training ------------



logger.info('------------ Begin training ------------')

best_valid_loss = 1e6
best_params = None
for epoch in range(epochs):
    # batch data
    if batch_size==1:
        train_data_batch = train_data[epoch, :, :]
        train_data_batch = train_data_batch.reshape(1, *train_data_batch.shape)
    else:
        inds = torch.randperm(num_rollouts)[:batch_size]
        # NOTE: use ranperm instead of randint to avoid repeated samples in a batch
        train_data_batch = train_data[inds, :, :]

    optimizer.zero_grad()
    # simulate over t_end steps
    x_log, _, u_log = sys.rollout(
        controller=ctl, data=train_data_batch, train=True,
    )
    x_log = x_log.reshape(batch_size, t_end, sys.num_states)
    u_log = u_log.reshape(batch_size, t_end, sys.num_inputs)

    # loss of this rollout
    loss = loss_fn.forward(x_log, u_log)
    msg = 'Epoch: %i --- Original train loss: %.2f'% (epoch, loss)


    # record state dict if best on valid
    if early_stopping and epoch%valid_period==0:
        # rollout the current controller on the calid data
        with torch.no_grad():
            x_log_valid, _, u_log_valid = sys.rollout(
                controller=ctl, data=valid_data, train=False,
            )
            x_log_valid = x_log_valid.reshape(valid_data.shape[0], t_end, sys.num_states)
            u_log_valid = u_log_valid.reshape(valid_data.shape[0], t_end, sys.num_inputs)
            # cost of the valid data
            loss_valid = loss_fn.forward(x_log_valid, u_log_valid)
        msg += ' ---||--- validation loss: %.2f' % (loss_valid.item())
        # compare with the best valid loss
        if loss_valid.item()<best_valid_loss:
            best_valid_loss = loss_valid.item()
            best_params = ctl.parameters_as_vector().detach().clone()
            msg += ' (best so far)'

    logger.info(msg)

    # Take a step
    # don't take a step at teh last epoch, b.c. o.w. last printed results and final results are different
    if epoch < epochs-1:
        loss.backward()
        optimizer.step()
        ctl.psi_u.update_model_param()

# ------ set to best seen during training ------
if early_stopping:
    ctl.set_parameters_as_vector(best_params)

# # ------ Save trained model ------
# fname = exp_name+'_emp_T'+str(t_end)+'_S'+str(num_rollouts)+'_stdini'+str(std_ini)+'_agents'+str(n_agents)+'_RS'+str(random_seed)+'.pt'
# file_path = os.path.join(BASE_DIR, 'experiments', 'robotsX', 'saved_results', 'trained_models')
# path_exist = os.path.exists(file_path)
# if not path_exist:
#     os.makedirs(file_path)
# filename = os.path.join(file_path, fname)
# res_dict = ctl.psi_u.state_dict()
# res_dict['num_rollouts'] = num_rollouts
# res_dict['Q'], res_dict['alpha_u'] = Q, alpha_u
# res_dict['alpha_ca'], res_dict['alpha_obst'] = alpha_ca, alpha_obst
# res_dict['n_xi'], res_dict['l'] = n_xi, l
# res_dict['initialization_std'] = initialization_std
# torch.save(res_dict, filename)

# logger.info('[INFO] saved trained model as: ' + fname)

# # ------ results on the entire train data ------

# logger.info('[INFO] evaluating the trained model on the entire train data.')
# with torch.no_grad():
#     x_log, _, u_log = sys.rollout(
#         controller=ctl, data=train_data, train=False,
#     )   # use the entire train data, not a batch
#     # evaluate losses
#     loss = loss_fn.forward(x_log, u_log)
#     msg = 'Final result: Original train loss: %.4f' % (loss)
#     if not bounded_loss_fn is None:
#         bounded_loss = bounded_loss_fn.forward(x_log, u_log)
#         msg += ', Bounded train loss: %.2f' % (bounded_loss)
#     logger.info(msg)

#     # count collisions
#     num_col = detect_collisions_multitraj(x_log, n_agents, min_dist)
#     per_col = percentage_collisions_multitraj(x_log, n_agents, min_dist)
#     logger.info('Number of collisions in train data = ' + str(num_col) + '. Percentage: ' + str(per_col*100) + '%')

# # ------------ 5. Test Dataset ------------

# logger.info('[INFO] evaluating the trained model on the test data.')
# with torch.no_grad():
#     # simulate over t_end steps
#     x_log, _, u_log = sys.rollout(
#         controller=ctl, data=test_data, train=False,
#     )
#     # loss
#     test_loss = loss_fn.forward(x_log, u_log).item()
#     msg = "True test loss : %.4f" % (test_loss)
#     if not bounded_loss_fn is None:
#         bounded_test_loss = bounded_loss_fn.forward(x_log, u_log).item()
#         msg += ', Bounded test loss: %.2f' % (bounded_test_loss)
#     msg += ' (approximated using {:3.0f} test rollouts).'.format(test_data.shape[0])
#     logger.info(msg)

# # count collisions
# num_col = detect_collisions_multitraj(x_log, n_agents, min_dist)
# per_col = percentage_collisions_multitraj(x_log, n_agents, min_dist)
# msg += '\nNumber of collisions in test data = ' + str(num_col) + '. Percentage: ' + str(per_col*100) + '%'
# logger.info(msg)