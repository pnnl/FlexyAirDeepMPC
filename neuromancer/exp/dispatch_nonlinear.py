import os
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('-hours', type=int, help='number of gpu hours to request for job', default=72)
parser.add_argument('-partition', type=str, help='Partition of gpus to access', default='shared_dlt')
parser.add_argument('-allocation', type=str, help='Allocation name for billing', default='deepmpc')
parser.add_argument('-env', type=str, help='Name of conda environment for running code.', default='mpc2')
parser.add_argument('-results', type=str, help='Where to log mlflow results', default='/qfs/projects/deepmpc/mlflow/nonlinear_exp_2020_7_15/mlruns')
parser.add_argument('-exp_folder', type=str, help='Where to save sbatch scripts and log files',
                    default='sbatch/')
parser.add_argument('-nsamples', type=int, help='Number of samples for each experimental configuration',
                    default=1)
args = parser.parse_args()

template = '#!/bin/bash\n' +\
           '#SBATCH -A %s\n' % args.allocation +\
           '#SBATCH -t %s:00:00\n' % args.hours +\
           '#SBATCH --gres=gpu:1\n' +\
           '#SBATCH -p %s\n' % args.partition +\
           '#SBATCH -N 1\n' +\
           '#SBATCH -n 2\n' +\
           '#SBATCH -o %j.out\n' +\
           '#SBATCH -e %j.err\n' +\
           'source /etc/profile.d/modules.sh\n' +\
           'module purge\n' +\
           'module load python/anaconda3.2019.3\n' +\
           'ulimit\n' +\
           'source activate %s\n\n' % args.env

os.system('mkdir %s' % args.exp_folder)
datatypes = ['emulator', 'emulator', 'emulator', 'emulator', 'datafile', 'datafile']
systems = ['CSTR', 'TwoTank', 'LorenzSystem', 'LotkaVolterra', 'aero', 'flexy_air']
linear_map = ['linear', 'pf', 'softSVD']
nonlinear_map = ['mlp', 'residual_mlp', 'rnn']
models = ['blackbox', 'hw', 'blocknlin']
Q_values = (0.2, 0.2, 0.2, 1.0, 1.0)
nsteps_range = [8]
os.system('mkdir temp')
for system, datatype in zip(systems, datatypes):
    for model in models:
        for bias in ['-bias']:
            for linear in linear_map:
                for nonlinear in nonlinear_map:
                    for nsteps in nsteps_range:
                        for i in range(args.nsamples):
                            cmd = 'python train.py ' +\
                                  '-gpu 0 ' + \
                                  '-lr 0.003 ' + \
                                  '-epochs 1000 ' + \
                                  '-nsim 10000 ' + \
                                  '-location %s ' % args.results + \
                                  '-system_data %s ' % datatype + \
                                  '-system %s ' % system + \
                                  '-linear_map %s ' % linear + \
                                  '-nonlinear_map %s ' % nonlinear + \
                                  '-state_estimator %s ' % nonlinear + \
                                  '-nsteps %s ' % nsteps + \
                                  '-logger mlflow ' + \
                                  '-ssm_type %s ' % model +\
                                  '-nx_hidden 10 ' + \
                                  '-n_layers 3 ' +\
                                  '%s ' % bias + \
                                  '-Q_con_x %s -Q_dx %s -Q_sub %s -Q_y %s -Q_e %s ' % Q_values + \
                                  '-exp %s ' % (system) + \
                                  '-savedir temp/%s_%s_%s_%s_%s_%s_%s ' % (system, model, bias, linear, nonlinear, nsteps, i)

                            with open(os.path.join(args.exp_folder, 'exp_%s_%s_%s_%s_%s_%s_%s.slurm' % (system, model, bias, linear, nonlinear, nsteps, i)), 'w') as cmdfile: # unique name for sbatch script
                                cmdfile.write(template + cmd)