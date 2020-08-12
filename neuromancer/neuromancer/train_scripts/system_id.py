"""
Script for training block dynamics models for system identification.
Current block structure supported are black_box, hammerstein, hammerstein-weiner, and general block non-linear

Basic model options are:
    + prior on the linear maps of the neural network
    + state estimator
    + non-linear map type
    + hidden state dimension
    + Whether to use affine or linear maps (bias term)
Basic data options are:
    + Load from a variety of premade data sequences
    + Load from a variety of emulators
    + Normalize input, output, or disturbance data
    + Nstep prediction horizon
Basic optimization options are:
    + Number of epochs to train on
    + Learn rate
Basic logging options are:
    + print to stdout
    + mlflow
    + weights and bias

More detailed description of options in the parse_args()
"""
# python base imports
import argparse

# machine learning data science imports
import torch
import torch.nn.functional as F
import torch.nn as nn

# code ecosystem imports
import slim

# local imports
from neuromancer.datasets import EmulatorDataset, FileDataset, systems
import neuromancer.dynamics as dynamics
import neuromancer.estimators as estimators
import neuromancer.blocks as blocks
import neuromancer.loggers as loggers
from neuromancer.visuals import VisualizerOpen, VisualizerTrajectories
from neuromancer.trainer import Trainer
from neuromancer.problem import Problem, Objective
from neuromancer.activations import BLU, SoftExponential
from neuromancer.simulators import OpenLoopSimulator

""" python system_id.py -system flexy_air -epochs 10 -nx_hidden 2
0 -ssm_type blackbox -state_estimator mlp -nonlinear_map residual_mlp -n_layers 2 -nsim 10000 -nsteps 32 -lr 0.001
Namespace(Q_con_fdu=0.0, Q_con_x=1.0, Q_dx=0.2, Q_e=1.0, Q_sub=0.2, Q_y=1.0, activation='gelu', bias=False, epochs=10, exp='test', gpu=None, linear_map='linear', location='mlrun
s', logger='stdout', lr=0.001, n_layers=2, nonlinear_map='residual_mlp', norm=['U', 'D', 'Y'], nsim=10000, nsteps=32
"""


def parse():
    parser = argparse.ArgumentParser()
    parser.add_argument('-gpu', type=int, default=None,
                        help="Gpu to use")
    # OPTIMIZATION PARAMETERS
    opt_group = parser.add_argument_group('OPTIMIZATION PARAMETERS')
    opt_group.add_argument('-epochs', type=int, default=5000)
    opt_group.add_argument('-lr', type=float, default=0.001,
                           help='Step size for gradient descent.')

    #################
    # DATA PARAMETERS
    data_group = parser.add_argument_group('DATA PARAMETERS')
    data_group.add_argument('-nsteps', type=int, default=32,
                            help='Number of steps for open loop during training.')
    data_group.add_argument('-system', type=str, default='Reno_ROM40', choices=list(systems.keys()),
                            help='select particular dataset with keyword')
    data_group.add_argument('-nsim', type=int, default=10000,
                            help='Number of time steps for full dataset. (ntrain + ndev + ntest)'
                                 'train, dev, and test will be split evenly from contiguous, sequential, '
                                 'non-overlapping chunks of nsim datapoints, e.g. first nsim/3 art train,'
                                 'next nsim/3 are dev and next nsim/3 simulation steps are test points.'
                                 'None will use a default nsim from the selected dataset or emulator')
    data_group.add_argument('-norm', nargs='+', default=['U', 'D', 'Y'], choices=['U', 'D', 'Y'],
                            help='List of sequences to max-min normalize')
    
    ##################
    # MODEL PARAMETERS
    model_group = parser.add_argument_group('MODEL PARAMETERS')
    model_group.add_argument('-ssm_type', type=str, choices=['blackbox', 'hw', 'hammerstein', 'blocknlin'],
                             default='blocknlin')
    model_group.add_argument('-nx_hidden', type=int, default=20, help='Number of hidden states per output')
    model_group.add_argument('-n_layers', type=int, default=2, help='Number of hidden layers of single time-step state transition')
    model_group.add_argument('-state_estimator', type=str,
                             choices=['rnn', 'mlp', 'linear', 'residual_mlp'], default='mlp')
    model_group.add_argument('-estimator_input_window', type=int, default=1,
                             help="Number of previous time steps measurements to include in state estimator input")
    model_group.add_argument('-linear_map', type=str, choices=list(slim.maps.keys()),
                             default='linear')
    model_group.add_argument('-nonlinear_map', type=str, default='residual_mlp',
                             choices=['mlp', 'rnn', 'pytorch_rnn', 'linear', 'residual_mlp'])
    model_group.add_argument('-bias', action='store_true', help='Whether to use bias in the neural network models.')
    model_group.add_argument('-activation', choices=['relu', 'gelu', 'blu', 'softexp'], default='gelu',
                             help='Activation function for neural networks')

    ##################
    # Weight PARAMETERS
    weight_group = parser.add_argument_group('WEIGHT PARAMETERS')
    weight_group.add_argument('-Q_con_x', type=float,  default=1.0, help='Hidden state constraints penalty weight.')
    weight_group.add_argument('-Q_dx', type=float,  default=0.2,
                              help='Penalty weight on hidden state difference in one time step.')
    weight_group.add_argument('-Q_sub', type=float,  default=0.2, help='Linear maps regularization weight.')
    weight_group.add_argument('-Q_y', type=float,  default=1.0, help='Output tracking penalty weight')
    weight_group.add_argument('-Q_e', type=float,  default=1.0, help='State estimator hidden prediction penalty weight')
    weight_group.add_argument('-Q_con_fdu', type=float,  default=0.0, help='Penalty weight on control actions and disturbances.')

    ####################
    # LOGGING PARAMETERS
    log_group = parser.add_argument_group('LOGGING PARAMETERS')
    log_group.add_argument('-savedir', type=str, default='test',
                           help="Where should your trained model and plots be saved (temp)")
    log_group.add_argument('-verbosity', type=int, default=1,
                           help="How many epochs in between status updates")
    log_group.add_argument('-exp', type=str, default='test',
                           help='Will group all run under this experiment name.')
    log_group.add_argument('-location', type=str, default='mlruns',
                           help='Where to write mlflow experiment tracking stuff')
    log_group.add_argument('-run', type=str, default='neuromancer',
                           help='Some name to tell what the experiment run was about.')
    log_group.add_argument('-logger', type=str, choices=['mlflow', 'stdout'], default='stdout',
                           help='Logging setup to use')
    log_group.add_argument('-train_visuals', action='store_true',
                           help='Whether to create visuals, e.g. animations during training loop')
    return parser


def logging(args):
    if args.logger == 'mlflow':
        Logger = loggers.MLFlowLogger(args=args, savedir=args.savedir, verbosity=args.verbosity,
                                      stdout=('nstep_dev_loss', 'loop_dev_loss', 'best_loop_dev_loss',
                                              'nstep_dev_ref_loss', 'loop_dev_ref_loss'))
        
    else:
        Logger = loggers.BasicLogger(args=args, savedir=args.savedir, verbosity=args.verbosity,
                                     stdout=('nstep_dev_loss', 'loop_dev_loss', 'best_loop_dev_loss',
                                     'nstep_dev_ref_loss', 'loop_dev_ref_loss'))
    device = f'cuda:{args.gpu}' if (args.gpu is not None) else 'cpu'
    return Logger, device


def dataset_load(args, device):
    if systems[args.system] == 'emulator':
        dataset = EmulatorDataset(system=args.system, nsim=args.nsim,
                                  norm=args.norm, nsteps=args.nsteps, device=device, savedir=args.savedir)
    else:
        dataset = FileDataset(system=args.system, nsim=args.nsim,
                              norm=args.norm, nsteps=args.nsteps, device=device, savedir=args.savedir)
    return dataset


if __name__ == '__main__':
    ###############################
    ########## LOGGING ############
    ###############################
    args = parse().parse_args()
    print({k: str(getattr(args, k)) for k in vars(args) if getattr(args, k)})
    logger, device = logging(args)

    ###############################
    ########## DATA ###############
    ###############################
    dataset = dataset_load(args, device)
    ##########################################
    ########## PROBLEM COMPONENTS ############
    ##########################################
    print(dataset.dims)
    nx = dataset.dims['Y'][-1]*args.nx_hidden

    activation = {'gelu': nn.GELU,
                  'relu': nn.ReLU,
                  'blu': BLU,
                  'softexp': SoftExponential}[args.activation]

    linmap = slim.maps[args.linear_map]

    nonlinmap = {'linear': linmap,
                 'mlp': blocks.MLP,
                 'rnn': blocks.RNN,
                 'pytorch_rnn': blocks.PytorchRNN,
                 'residual_mlp': blocks.ResMLP}[args.nonlinear_map]

    estimator = {'linear': estimators.LinearEstimator,
                 'mlp': estimators.MLPEstimator,
                 'rnn': estimators.RNNEstimator,
                 'residual_mlp': estimators.ResMLPEstimator
                 }[args.state_estimator]({**dataset.dims, 'x0': (nx,)},
                                         nsteps=args.nsteps,
                                         window_size=args.estimator_input_window,
                                         bias=args.bias,
                                         Linear=linmap,
                                         nonlin=activation,
                                         hsizes=[nx] * args.n_layers,
                                         input_keys=['Yp'],
                                         linargs=dict(),
                                         name='estim')

    dynamics_model = {'blackbox': dynamics.blackbox,
                      'blocknlin': dynamics.blocknlin,
                      'hammerstein': dynamics.hammerstein,
                      'hw': dynamics.hw}[args.ssm_type](args.bias, linmap, nonlinmap, {**dataset.dims, 'x0_estim': (nx,)},
                                                        n_layers=args.n_layers,
                                                        activation=activation,
                                                        name='dynamics',
                                                        input_keys={'x0': f'x0_{estimator.name}'})

    components = [estimator, dynamics_model]

    ##########################################
    ########## MULTI-OBJECTIVE LOSS ##########
    ##########################################
    estimator_loss = Objective(['X_pred', 'x0'],
                                lambda X_pred, x0: F.mse_loss(X_pred[-1, :-1, :], x0[1:]),
                                weight=args.Q_e, name='arrival_cost')
    regularization = Objective([f'reg_error_estim', f'reg_error_dynamics'],
                               lambda reg1, reg2: reg1 + reg2, weight=args.Q_sub, name='reg_error')
    reference_loss = Objective(['Y_pred_dynamics', 'Yf'], F.mse_loss, weight=args.Q_y,
                                name='ref_loss')
    state_smoothing = Objective(['X_pred_dynamics'], lambda x: F.mse_loss(x[1:], x[:-1]), weight=args.Q_dx,
                                name='state_smoothing')
    observation_lower_bound_penalty = Objective(['Y_pred_dynamics'],
                                                lambda x: torch.mean(F.relu(-x + 0.2)), weight=args.Q_con_x,
                                                name='y_low_bound_error')
    observation_upper_bound_penalty = Objective(['Y_pred_dynamics'],
                                                lambda x: torch.mean(F.relu(x - 1.2)), weight=args.Q_con_x,
                                                name='y_up_bound_error')

    objectives = [regularization, reference_loss]
    constraints = [state_smoothing, observation_lower_bound_penalty, observation_upper_bound_penalty]

    if args.ssm_type != 'blackbox':
        if 'U' in dataset.data:
            inputs_max_influence_lb = Objective(['fU_dynamics'], lambda x: torch.mean(F.relu(-x + 0.05)),
                                                  weight=args.Q_con_fdu,
                                                name='input_influence_lb')
            inputs_max_influence_ub = Objective(['fU_dynamics'], lambda x: torch.mean(F.relu(x - 0.05)),
                                                weight=args.Q_con_fdu, name='input_influence_ub')
            constraints += [inputs_max_influence_lb, inputs_max_influence_ub]
        if 'D' in dataset.data:
            disturbances_max_influence_lb = Objective([f'fD_dynamics'], lambda x: torch.mean(F.relu(-x + 0.05)),
                                                      weight=args.Q_con_fdu, name='dist_influence_lb')
            disturbances_max_influence_ub = Objective([f'fD_dynamics'], lambda x: torch.mean(F.relu(x - 0.05)),
                                                      weight=args.Q_con_fdu, name='dist_influence_ub')
            constraints += [disturbances_max_influence_lb, disturbances_max_influence_ub]

    ##########################################
    ########## OPTIMIZE SOLUTION ############
    ##########################################
    model = Problem(objectives, constraints, components).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    visualizer = VisualizerOpen(dataset, dynamics_model, args.verbosity, args.savedir)
    simulator = OpenLoopSimulator(model=model, dataset=dataset)
    trainer = Trainer(model, dataset, optimizer, logger=logger, visualizer=visualizer,
                      simulator=simulator, epochs=args.epochs)
    best_model = trainer.train()
    trainer.evaluate(best_model)
    logger.clean_up()
