"""
Script for training control policy

# TODO: constraints and objectives only on subset of variables
# TODO: online learning with subset of the parameter updates
# TODO: slack variables as policy featues

# TODO:
    # # OPTIONAL: plot problem graph of component connections
    # # maps internal component intecations via by mapping: output_keys -> input_keys
    # n_in = len(input_keys)
    # n_out = len(output_keys)
    # ModelConnections = np.zeros([n_in, n_out])
    # for i, k in enumerate(input_keys):
    #     if k in output_keys:
    #         ModelConnections[i, output_keys.index(k)] = 1

More detailed description of options in the parse_args()
"""
# import matplotlib
# matplotlib.use("Agg")
import argparse
import torch
from datasets import EmulatorDataset, FileDataset, Dataset
import dynamics
import estimators
import emulators
import policies
import linear
import blocks
import logger
from visuals import Visualizer, VisualizerTrajectories, VisualizerClosedLoop
from trainer import Trainer
from problem import Problem, Objective
import torch.nn.functional as F
import numpy as np
from simulators import OpenLoopSimulator, ClosedLoopSimulator
import dill

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-gpu', type=str, default=None,
                        help="Gpu to use")
    # OPTIMIZATION PARAMETERS
    opt_group = parser.add_argument_group('OPTIMIZATION PARAMETERS')
    opt_group.add_argument('-epochs', type=int, default=1000)
    opt_group.add_argument('-lr', type=float, default=0.003,
                           help='Step size for gradient descent.')

    #################
    # DATA PARAMETERS
    data_group = parser.add_argument_group('DATA PARAMETERS')
    data_group.add_argument('-nsteps', type=int, default=32,
                            help='control policy prediction horizon.')
    data_group.add_argument('-system_data', type=str, choices=['emulator', 'datafile'],
                            default='datafile',
                            help='source type of the dataset')
    data_group.add_argument('-system', default='flexy_air',
                            help='select particular dataset with keyword')
    data_group.add_argument('-nsim', type=int, default=6000,
                            help='Number of time steps for full dataset. (ntrain + ndev + ntest)'
                                 'train, dev, and test will be split evenly from contiguous, sequential, '
                                 'non-overlapping chunks of nsim datapoints, e.g. first nsim/3 art train,'
                                 'next nsim/3 are dev and next nsim/3 simulation steps are test points.'
                                 'None will use a default nsim from the selected dataset or emulator')
    data_group.add_argument('-norm', choices=['UDY', 'U', 'Y', None], type=str, default='UDY')
    data_group.add_argument('-dataset_name', type=str, choices=['openloop', 'closedloop'],
                            default='closedloop',
                            help='name of the dataset')


    ##################
    # MODEL PARAMETERS
    model_group = parser.add_argument_group('MODEL PARAMETERS')
    model_group.add_argument('-n_hidden', type=int, default=10, help='Number of hidden states')
    model_group.add_argument('-n_layers', type=int, default=4,
                             help='Number of hidden layers of single time-step state transition')
    model_group.add_argument('-policy', type=str,
                             choices=['rnn', 'mlp', 'linear'], default='mlp')
    model_group.add_argument('-linear_map', type=str, choices=list(linear.maps.keys()),
                             default='linear')
    model_group.add_argument('-bias', action='store_true', help='Whether to use bias in the neural network models.')
    model_group.add_argument('-policy_features', nargs='+', default=['x0'], help='Policy features')

    # to recreate model
    model_group.add_argument('-ssm_type', type=str, choices=['blackbox', 'hw', 'hammerstein', 'blocknlin'],
                             default='blackbox')
    model_group.add_argument('-nx_hidden', type=int, default=20, help='Number of hidden states per output')
    model_group.add_argument('-state_estimator', type=str,
                             choices=['rnn', 'mlp', 'linear', 'residual_mlp'], default='mlp')
    model_group.add_argument('-linear_map_model', type=str, choices=list(linear.maps.keys()),
                             default='linear')
    model_group.add_argument('-nonlinear_map', type=str, default='residual_mlp',
                             choices=['mlp', 'rnn', 'linear', 'residual_mlp'])


    ##################
    # Weight PARAMETERS
    weight_group = parser.add_argument_group('WEIGHT PARAMETERS')
    weight_group.add_argument('-Q_con_y', type=float, default=10.0, help='Output constraints penalty weight.')
    weight_group.add_argument('-Q_con_u', type=float, default=10.0, help='Input constraints penalty weight.')
    weight_group.add_argument('-Q_sub', type=float, default=0.2, help='Linear maps regularization weight.')
    weight_group.add_argument('-Q_r', type=float, default=1.0, help='Reference tracking penalty weight')
    weight_group.add_argument('-Q_du', type=float, default=1.0, help='Reference tracking penalty weight')

    ####################
    # LOGGING PARAMETERS
    log_group = parser.add_argument_group('LOGGING PARAMETERS')
    log_group.add_argument('-savedir', type=str, default='test',
                           help="Where should your trained model and plots be saved (temp)")
    log_group.add_argument('-verbosity', type=int, default=100,
                           help="How many epochs in between status updates")
    log_group.add_argument('-exp', default='test',
                           help='Will group all run under this experiment name.')
    log_group.add_argument('-location', default='mlruns',
                           help='Where to write mlflow experiment tracking stuff')
    log_group.add_argument('-run', default='deepmpc',
                           help='Some name to tell what the experiment run was about.')
    log_group.add_argument('-logger', choices=['mlflow', 'stdout'], default='stdout',
                           help='Logging setup to use')
    return parser.parse_args()


def logging(args):
    if args.logger == 'mlflow':
        Logger = logger.MLFlowLogger(args, args.savedir, args.verbosity)
    else:
        Logger = logger.BasicLogger(savedir=args.savedir, verbosity=args.verbosity)
    device = f'cuda:{args.gpu}' if (args.gpu is not None) else 'cpu'
    return Logger, device


def dataset_load(args, device, sequences=dict()):
    if args.system_data == 'emulator':
        dataset = EmulatorDataset(system=args.system, nsim=args.nsim, sequences=sequences, batch_type='mh',
                                  name=args.dataset_name,
                                  norm=args.norm, nsteps=args.nsteps, device=device, savedir=args.savedir)
    else:
        dataset = FileDataset(system=args.system, nsim=args.nsim, sequences=sequences, batch_type='mh',
                              name=args.dataset_name,
                              norm=args.norm, nsteps=args.nsteps, device=device, savedir=args.savedir)
    return dataset


if __name__ == '__main__':
    ###############################
    ########## LOGGING ############
    ###############################
    args = parse_args()
    logger, device = logging(args)

    ###############################
    ########## DATA ###############
    ###############################
    dataset = dataset_load(args, device)
    # select only first output (position)
    dataset.data['Y'] = dataset.data['Y'][:, 0].reshape(-1, 1)
    dataset.min_max_norms['Ymin'] = dataset.min_max_norms['Ymin'][0]
    dataset.min_max_norms['Ymax'] = dataset.min_max_norms['Ymax'][0]
    dataset.make_nstep(overwrite=True)
    dataset.make_loop()

    nsim, ny = dataset.data['Y'].shape
    nu = dataset.data['U'].shape[1]
    # new_sequences = {'Y_max': 0.8*np.ones([nsim, ny]), 'Y_min': 0.2*np.ones([nsim, ny]),
    #                  'U_max': np.ones([nsim, nu]), 'U_min': np.zeros([nsim, nu]),
    #                  'R': emulators.Periodic(nx=ny, nsim=nsim, numPeriods=12, xmax=1, xmin=0),
    #                  'Y_ctrl_': emulators.Periodic(nx=ny, nsim=nsim, numPeriods=30, xmax=1.0, xmin=0.0)}
    new_sequences = {'Y_max': 0.8 * np.ones([nsim, ny]), 'Y_min': 0.2 * np.ones([nsim, ny]),
                     'U_max': np.ones([nsim, nu]), 'U_min': np.zeros([nsim, nu]),
                     'R': emulators.Periodic(nx=ny, nsim=nsim, numPeriods=12, xmax=1, xmin=0),
                     'Y_ctrl_': emulators.WhiteNoise(nx=ny, nsim=nsim,  xmax=1.0, xmin=0.0)}
    # Y_ctrl_  - sampled state space
    dataset.add_data(new_sequences)
    dataset.make_nstep()
    dataset.make_loop()

    ##########################################
    ########## PROBLEM COMPONENTS ############
    ##########################################
    nx = dataset.dims['Y']*args.nx_hidden
    nu = dataset.dims['U'] if 'U' in dataset.dims else 0
    nd = dataset.dims['D'] if 'D' in dataset.dims else 0
    ny = dataset.dims['Y']
    dataset.add_variable({'x0': nx, 'U_pred': nu})

    # recreate dynamics components
    linmap_model = linear.maps[args.linear_map_model]
    nonlinmap = {'linear': linmap_model,
                 'mlp': blocks.MLP,
                 'rnn': blocks.RNN,
                 'residual_mlp': blocks.ResMLP}[args.nonlinear_map]

    if True:
        best_model = torch.load('./flexy_test/best_model.pth', pickle_module=dill)
        for k in range(len(best_model.components)):
            if best_model.components[k].name == 'dynamics':
                dynamics_model = best_model.components[k]
                dynamics_model.input_keys[2] = 'U_pred'
            if best_model.components[k].name == 'estim':
                estimator = best_model.components[k]
                estimator.input_keys[0] = 'Y_ctrl_p'
    else:
        # state space model setup
        if args.ssm_type == 'blackbox':
            dyn_output_keys = ['X_pred', 'Y_pred']
        else:
            dyn_output_keys = ['X_pred', 'Y_pred', 'fU', 'fD']
        dynamics_model = {'blackbox': dynamics.blackbox,
                          'blocknlin': dynamics.blocknlin,
                          'hammerstein': dynamics.hammerstein,
                          'hw': dynamics.hw
                          }[args.ssm_type](args.bias, linmap_model, nonlinmap, nx, nu, nd, ny,
                                           n_layers=args.n_layers,
                                           input_keys=['Yf', 'x0', 'U_pred', 'Df'],
                                           output_keys=dyn_output_keys,
                                           name='dynamics')
        # state estimator setup
        estimator = {'linear': estimators.LinearEstimator,
                     'mlp': estimators.MLPEstimator,
                     'rnn': estimators.RNNEstimator,
                     'residual_mlp': estimators.ResMLPEstimator
                     }[args.state_estimator](dataset.dims,
                                             nsteps=args.nsteps,
                                             bias=args.bias,
                                             Linear=linmap_model,
                                             nonlin=F.gelu,
                                             hsizes=[nx] * args.n_layers,
                                             input_keys=['Y_ctrl_p'],
                                             output_keys=['x0'],
                                             linargs=dict(),
                                             name='estim')

    # don't update learned model parameters
    dynamics_model.requires_grad_(False)
    estimator.requires_grad_(False)

    nh_policy = args.n_hidden
    linmap = linear.maps[args.linear_map]

    # control policy setup
    policy = {'linear': policies.LinearPolicy,
                 'mlp': policies.MLPPolicy,
                 'rnn': policies.RNNPolicy
              }[args.policy](dataset.dims,
               nsteps=args.nsteps,
               bias=args.bias,
               Linear=linmap,
               nonlin=F.gelu,
               hsizes=[nh_policy] * args.n_layers,
               input_keys=['x0', 'Rf', 'Df'],
               output_keys=['U_pred'],
               linargs=dict(),
               name='policy')

    components = [estimator, policy, dynamics_model]

    # component variables
    input_keys = list(set.union(*[set(comp.input_keys) for comp in components]))
    output_keys = list(set.union(*[set(comp.output_keys) for comp in components]))
    dataset_keys = list(set(dataset.train_data.keys()))
    plot_keys = ['Y_pred', 'U_pred']  # variables to be plotted

    ##########################################
    ########## MULTI-OBJECTIVE LOSS ##########
    ##########################################
    regularization = Objective(['policy_reg_error'], lambda reg: reg,
                               weight=args.Q_sub)
    reference_loss = Objective(['Y_pred', 'Rf'], F.mse_loss, weight=args.Q_r)
    control_smoothing = Objective(['U_pred'], lambda x: F.mse_loss(x[1:], x[:-1]), weight=args.Q_du)
    observation_lower_bound_penalty = Objective(['Y_pred', 'Y_minf'], lambda x, xmin: torch.mean(F.relu(-x + xmin)),
                                                weight=args.Q_con_y)
    observation_upper_bound_penalty = Objective(['Y_pred', 'Y_maxf'], lambda x, xmax: torch.mean(F.relu(x - xmax)),
                                                weight=args.Q_con_y)
    inputs_lower_bound_penalty = Objective(['U_pred', 'U_minf'], lambda x, xmin: torch.mean(F.relu(-x + xmin)),
                                                weight=args.Q_con_u)
    inputs_upper_bound_penalty = Objective(['U_pred', 'U_maxf'], lambda x, xmax: torch.mean(F.relu(x - xmax)),
                                           weight=args.Q_con_u)

    objectives = [regularization, reference_loss]
    constraints = [observation_lower_bound_penalty, observation_upper_bound_penalty,
                   inputs_lower_bound_penalty, inputs_upper_bound_penalty]

    ##########################################
    ########## OPTIMIZE SOLUTION ############
    ##########################################
    model = Problem(objectives, constraints, components).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    visualizer = VisualizerClosedLoop(dataset, dynamics_model, plot_keys, args.verbosity)
    emulator = emulators.systems[args.system]() if args.system_data == 'emulator' \
        else dynamics_model if args.system_data == 'datafile' else None
    simulator = ClosedLoopSimulator(model=model, dataset=dataset, emulator=emulator)
    trainer = Trainer(model, dataset, optimizer, logger=logger, visualizer=visualizer,
                      simulator=simulator, epochs=args.epochs)
    best_model = trainer.train()
    trainer.evaluate(best_model)
    logger.clean_up()
