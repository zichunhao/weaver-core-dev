#!/usr/bin/env python

import os
import ast
import sys
import shutil
import glob
import argparse
import functools
import numpy as np
import math
import torch

from torch.utils.data import DataLoader
from utils.logger import _logger, _configLogger
from utils.dataset import SimpleIterDataset
from utils.import_tools import import_module

torch.autograd.set_detect_anomaly(True)

parser = argparse.ArgumentParser()
parser.add_argument('--train-mode', type=str, default='cls',
                    choices=['cls', 'regression', 'hybrid', 'custom'],
                    help='training mode')
parser.add_argument('--train-mode-params', type=str, default='',
                    choices=['', 'metric:loss'],
                    help='training mode parameters')
parser.add_argument('--run-mode', type=str, default='default',
                    choices=['default', 'train-only', 'val-only'],
                    help='training mode')
parser.add_argument('--early-stop', action='store_true', default=False,
                    help='Early stop training if the validation metric does not improve for a few epochs')
parser.add_argument('--early-stop-dlr', type=float, default=5e-4,
                    help='The delta loss for early stopping')
parser.add_argument('--use-last-model', action='store_true', default=False,
                    help='Simply take the last model as the best model')
parser.add_argument('--extra-selection', type=str, default=None,
                    help='Additional selection requirement, will modify `selection` to `(selection) & (extra)` on-the-fly')
parser.add_argument('--seed', type=int, default=-1,
                    help='Set seed for all torch, numpy utilities for reproducibility')
parser.add_argument('-c', '--data-config', type=str, default='data/ak15_points_pf_sv_v0.yaml',
                    help='data config YAML file')
parser.add_argument('-i', '--data-train', nargs='*', default=[],
                    help='training files; supported syntax:'
                         ' (a) plain list, `--data-train /path/to/a/* /path/to/b/*`;'
                         ' (b) (named) groups [Recommended], `--data-train a:/path/to/a/* b:/path/to/b/*`,'
                         ' the file splitting (for each dataloader worker) will be performed per group,'
                         ' and then mixed together, to ensure a uniform mixing from all groups for each worker.'
                    )
parser.add_argument('-l', '--data-val', nargs='*', default=[],
                    help='validation files; when not set, will use training files and split by `--train-val-split`')
parser.add_argument('-t', '--data-test', nargs='*', default=[],
                    help='testing files; supported syntax:'
                         ' (a) plain list, `--data-test /path/to/a/* /path/to/b/*`;'
                         ' (b) keyword-based, `--data-test a:/path/to/a/* b:/path/to/b/*`, will produce output_a, output_b;'
                         ' (c) split output per N input files, `--data-test a%10:/path/to/a/*`, will split per 10 input files')
parser.add_argument('--data-fraction', type=float, default=1,
                    help='fraction of events to load from each file; for training, the events are randomly selected for each epoch')
parser.add_argument('--data-split-group', type=int, default=1,
                    help='number of groups to split the dataset when loading. This helps to mitigate the memory usage of dataloader when a number larger than 1 is specified')
parser.add_argument('--file-fraction', type=float, default=1,
                    help='fraction of files to load; for training, the files are randomly selected for each epoch')
parser.add_argument('--fetch-by-files', action='store_true', default=False,
                    help='When enabled, will load all events from a small number (set by ``--fetch-step``) of files for each data fetching. '
                         'Otherwise (default), load a small fraction of events from all files each time, which helps reduce variations in the sample composition.')
parser.add_argument('--fetch-by-files-threshold', type=int, default=5 * 1024 * 1024 * 1024,
                    help='threshold (in bytes) to determine whether to load all events from all files or a fraction of events from all files. Deafult: 5GB')
parser.add_argument('--fetch-step', type=float, default=0.01,
                    help='fraction of events to load each time from every file (when ``--fetch-by-files`` is disabled); '
                         'Or: number of files to load each time (when ``--fetch-by-files`` is enabled). Shuffling & sampling is done within these events, so set a large enough value.')
parser.add_argument('--in-memory', action='store_true', default=False,
                    help='load the whole dataset (and perform the preprocessing) only once and keep it in memory for the entire run')
parser.add_argument('--train-val-split', type=float, default=0.8,
                    help='training/validation split fraction')
parser.add_argument('--test-range', type=float, nargs=2, default=[0, 1],
                    help='test dataset range')
parser.add_argument('--demo', action='store_true', default=False,
                    help='quickly test the setup by running over only a small number of events')
parser.add_argument('--lr-finder', type=str, default=None,
                    help='run learning rate finder instead of the actual training; format: ``start_lr, end_lr, num_iters``')
parser.add_argument('--tensorboard', type=str, default=None,
                    help='create a tensorboard summary writer with the given comment')
parser.add_argument('--tensorboard-custom-fn', type=str, default=None,
                    help='the path of the python script containing a user-specified function `get_tensorboard_custom_fn`, '
                         'to display custom information per mini-batch or per epoch, during the training, validation or test.')
parser.add_argument('-n', '--network-config', type=str, default='networks/particle_net_pfcand_sv.py',
                    help='network architecture configuration file; the path must be relative to the current dir')
parser.add_argument('-o', '--network-option', nargs=2, action='append', default=[],
                    help='options to pass to the model class constructor, e.g., `--network-option use_counts False`')
parser.add_argument('-m', '--model-prefix', type=str, default='models/{auto}/network',
                    help='path to save or load the model; for training, this will be used as a prefix, so model snapshots '
                         'will saved to `{model_prefix}_epoch-%d_state.pt` after each epoch, and the one with the best '
                         'validation metric to `{model_prefix}_best_epoch_state.pt`; for testing, this should be the full path '
                         'including the suffix, otherwise the one with the best validation metric will be used; '
                         'for training, `{auto}` can be used as part of the path to auto-generate a name, '
                         'based on the timestamp and network configuration')
parser.add_argument('--load-model-weights', type=str, default=None,
                    help='initialize model with pre-trained weights')
parser.add_argument('--num-epochs', type=int, default=20,
                    help='number of epochs')
parser.add_argument('--steps-per-epoch', type=int, default=None,
                    help='number of steps (iterations) per epochs; '
                         'if neither of `--steps-per-epoch` or `--samples-per-epoch` is set, each epoch will run over all loaded samples')
parser.add_argument('--steps-per-epoch-val', type=int, default=None,
                    help='number of steps (iterations) per epochs for validation; '
                         'if neither of `--steps-per-epoch-val` or `--samples-per-epoch-val` is set, each epoch will run over all loaded samples')
parser.add_argument('--samples-per-epoch', type=int, default=None,
                    help='number of samples per epochs; '
                         'if neither of `--steps-per-epoch` or `--samples-per-epoch` is set, each epoch will run over all loaded samples')
parser.add_argument('--samples-per-epoch-val', type=int, default=None,
                    help='number of samples per epochs for validation; '
                         'if neither of `--steps-per-epoch-val` or `--samples-per-epoch-val` is set, each epoch will run over all loaded samples')
parser.add_argument('--optimizer', type=str, default='ranger', choices=['adam', 'adamW', 'radam', 'ranger', 'sgd'],  # TODO: add more
                    help='optimizer for the training')
parser.add_argument('--optimizer-option', nargs=2, action='append', default=[],
                    help='options to pass to the optimizer class constructor, e.g., `--optimizer-option weight_decay 1e-4`')
parser.add_argument('--lr-scheduler', type=str, default='flat+decay',
                    choices=['none', 'steps', 'flat+decay', 'flat+linear', 'flat+cos', 'one-cycle', 'cosanneal'],
                    help='learning rate scheduler')
parser.add_argument('--warmup-steps', type=int, default=0,
                    help='number of warm-up steps, only valid for `flat+linear` and `flat+cos` lr schedulers')
parser.add_argument('--load-epoch', type=int, default=None,
                    help='used to resume interrupted training, load model and optimizer state saved in the `epoch-%d_state.pt` and `epoch-%d_optimizer.pt` files')
parser.add_argument('--start-lr', type=float, default=5e-3,
                    help='start learning rate')
parser.add_argument('--batch-size', type=int, default=128,
                    help='batch size')
parser.add_argument('--use-amp', action='store_true', default=False,
                    help='use mixed precision training (fp16)')
parser.add_argument('--compile-model', action='store_true', default=False,
                    help='compile model (supported from PyTorch 2.0)')
parser.add_argument('--gpus', type=str, default='0',
                    help='device for the training/testing; to use CPU, set to empty string (""); to use multiple gpu, set it as a comma separated list, e.g., `1,2,3,4`')
parser.add_argument('--predict-gpus', type=str, default=None,
                    help='device for the testing; to use CPU, set to empty string (""); to use multiple gpu, set it as a comma separated list, e.g., `1,2,3,4`; if not set, use the same as `--gpus`')
parser.add_argument('--num-workers', type=int, default=1,
                    help='number of threads to load the dataset; memory consumption and disk access load increases (~linearly) with this numbers')
parser.add_argument('--predict', action='store_true', default=False,
                    help='run prediction instead of training')
parser.add_argument('--predict-output', type=str,
                    help='path to save the prediction output, support `.root` and `.parquet` format')
parser.add_argument('--export-onnx', type=str, default=None,
                    help='export the PyTorch model to ONNX model and save it at the given path (path must ends w/ .onnx); '
                         'needs to set `--data-config`, `--network-config`, and `--model-prefix` (requires the full model path)')
parser.add_argument('--io-test', action='store_true', default=False,
                    help='test throughput of the dataloader')
parser.add_argument('--copy-inputs', action='store_true', default=False,
                    help='copy input files to the current dir (can help to speed up dataloading when running over remote files, e.g., from EOS)')
parser.add_argument('--log-file', type=str, default='',
                    help='path to the log file; `{auto}` can be used as part of the path to auto-generate a name, based on the timestamp and network configuration')
parser.add_argument('--log-level', type=str, default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                    help='log level')
parser.add_argument('--print', action='store_true', default=False,
                    help='do not run training/prediction but only print model information, e.g., FLOPs and number of parameters of a model')
parser.add_argument('--profile', action='store_true', default=False,
                    help='run the profiler')
parser.add_argument('--backend', type=str, choices=['gloo', 'nccl', 'mpi'], default=None,
                    help='backend for distributed training')


def to_filelist(args, mode='train'):
    if mode == 'train':
        flist = args.data_train
    elif mode == 'val':
        flist = args.data_val
    else:
        raise NotImplementedError('Invalid mode %s' % mode)

    # keyword-based: 'a:/path/to/a b:/path/to/b'
    file_dict = {}
    for f in flist:
        if ':' in f:
            name, fp = f.split(':')
        else:
            name, fp = '_', f
        files = glob.glob(fp)
        if name in file_dict:
            file_dict[name] += files
        else:
            file_dict[name] = files

    # sort files
    for name, files in file_dict.items():
        file_dict[name] = sorted(files)

    if args.local_rank is not None:
        if mode == 'train':
            local_world_size = int(os.environ['LOCAL_WORLD_SIZE'])
            new_file_dict = {}
            for name, files in file_dict.items():
                new_files = files[args.local_rank::local_world_size]
                assert(len(new_files) > 0)
                np.random.shuffle(new_files)
                new_file_dict[name] = new_files
            file_dict = new_file_dict

    if args.copy_inputs:
        import tempfile
        tmpdir = tempfile.mkdtemp()
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir)
        new_file_dict = {name: [] for name in file_dict}
        for name, files in file_dict.items():
            for src in files:
                dest = os.path.join(tmpdir, src.lstrip('/'))
                if not os.path.exists(os.path.dirname(dest)):
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                _logger.info('Copied file %s to %s' % (src, dest))
                new_file_dict[name].append(dest)
            if len(files) != len(new_file_dict[name]):
                _logger.error('Only %d/%d files copied for %s file group %s',
                              len(new_file_dict[name]), len(files), mode, name)
        file_dict = new_file_dict

    filelist = sum(file_dict.values(), [])
    assert(len(filelist) == len(set(filelist)))
    return file_dict, filelist


def train_load(args):
    """
    Loads the training data.
    :param args:
    :return: train_loader, val_loader, data_config, train_inputs
    """

    train_file_dict, train_files = to_filelist(args, 'train')
    if len(train_files) == 0:
        raise RuntimeError('No files found for training!')
    if args.data_val:
        val_file_dict, val_files = to_filelist(args, 'val')
        train_range = val_range = (0, 1)
        if len(val_files) == 0:
            raise RuntimeError('No files found for validation!')
    else:
        val_file_dict, val_files = train_file_dict, train_files
        train_range = (0, args.train_val_split)
        val_range = (args.train_val_split, 1)
    _logger.info('Using %d files for training, range: %s' % (len(train_files), str(train_range)))
    _logger.debug(f'Training files: {train_files}')
    _logger.info('Using %d files for validation, range: %s' % (len(val_files), str(val_range)))
    _logger.debug(f'Validation files: {val_files}')

    if args.demo:
        train_files = train_files[:20]
        val_files = val_files[:20]
        train_file_dict = {'_': train_files}
        val_file_dict = {'_': val_files}
        _logger.info(train_files)
        _logger.info(val_files)
        args.data_fraction = 0.1
        args.data_split_group = 1
        args.fetch_step = 0.002

    if args.in_memory and (args.steps_per_epoch is None or args.steps_per_epoch_val is None):
        raise RuntimeError('Must set --steps-per-epoch when using --in-memory!')

    def get_dataset(file_dict, files, range, name, size_threshold=5 * 1024 * 1024 * 1024):
        total_size = sum(os.path.getsize(f) for f in files)
        if total_size < size_threshold:
            fetch_by_files = True
            fetch_step = 1
            _logger.info(
                f"Small dataset: {total_size / 1024 / 1024:.2f} MB < {size_threshold / 1024 / 1024:.2f} MB; load all events from all files"
            )
        else:
            fetch_by_files = args.fetch_by_files
            fetch_step = args.fetch_step
            _logger.info(f"Large dataset: {total_size / 1024 / 1024:.2f} MB >= {size_threshold / 1024 / 1024:.2f} MB; keep the fetch_by_files={fetch_by_files} and fetch_step={fetch_step} settings")

        return SimpleIterDataset(
            file_dict, args.data_config, for_training=True,
            extra_selection=args.extra_selection,
            load_range_and_fraction=(range, args.data_fraction, args.data_split_group),
            file_fraction=args.file_fraction,
            fetch_by_files=fetch_by_files,
            fetch_step=fetch_step,
            infinity_mode=args.steps_per_epoch is not None,
            in_memory=args.in_memory,
            name=name + ('' if args.local_rank is None else f'_rank{args.local_rank}')
        )

    size_threshold = args.fetch_by_files_threshold
    train_data = get_dataset(train_file_dict, train_files, train_range, 'train', size_threshold)
    val_data = get_dataset(val_file_dict, val_files, val_range, 'val', size_threshold)

    train_loader = DataLoader(train_data, batch_size=args.batch_size, drop_last=True, pin_memory=True,
                              num_workers=min(args.num_workers, int(len(train_files) * args.file_fraction)),
                              persistent_workers=args.num_workers > 0 and args.steps_per_epoch is not None)
    val_loader = DataLoader(val_data, batch_size=args.batch_size, drop_last=True, pin_memory=True,
                            num_workers=min(args.num_workers, int(len(val_files) * args.file_fraction)),
                            persistent_workers=args.num_workers > 0 and args.steps_per_epoch_val is not None)
    data_config = train_data.config
    train_input_names = train_data.config.input_names
    train_label_names = train_data.config.label_names

    return train_loader, val_loader, data_config, train_input_names, train_label_names


def test_load(args):
    """
    Loads the test data.
    :param args:
    :return: test_loaders, data_config
    """
    # keyword-based --data-test: 'a:/path/to/a b:/path/to/b'
    # split --data-test: 'a%10:/path/to/a/*'
    file_dict = {}
    split_dict = {}
    for f in args.data_test:
        if ':' in f:
            name, fp = f.split(':')
            if '%' in name:
                name, split = name.split('%')
                split_dict[name] = int(split)
        else:
            name, fp = '', f
        files = glob.glob(fp)
        if name in file_dict:
            file_dict[name] += files
        else:
            file_dict[name] = files

    # sort files
    for name, files in file_dict.items():
        file_dict[name] = sorted(files)

    # apply splitting
    for name, split in split_dict.items():
        files = file_dict.pop(name)
        for i in range((len(files) + split - 1) // split):
            file_dict[f'{name}_{i}'] = files[i * split:(i + 1) * split]

    def get_test_loader(name, size_threshold=5 * 1024 * 1024 * 1024):
        filelist = file_dict[name]
        _logger.info('Running on test file group %s with %d files:\n...%s', name, len(filelist), '\n...'.join(filelist))
        num_workers = min(args.num_workers, len(filelist))
        _logger.info(f"Using {num_workers} workers for test data loading")
        _logger.info(f"Instantiating test dataset for {name}")
        # check the total size of the test dataset
        total_size = 0
        for f in filelist:
            total_size += os.path.getsize(f)
        if total_size < size_threshold:
            # small dataset, load all events from all files
            _logger.info(
                f"Small dataset: {total_size / 1024 / 1024:.2f} MB < {size_threshold / 1024 / 1024:.2f} MB; load all events from all files"
            )
            fetch_by_files = True
            fetch_step = 1
        else:
            # large dataset, follow the fetch_by_files and fetch_step settings
            fetch_by_files = args.fetch_by_files
            fetch_step = args.fetch_step
            _logger.info(f"Large dataset: {total_size / 1024 / 1024:.2f} MB >= {size_threshold / 1024 / 1024:.2f} MB; follow the fetch_by_files={fetch_by_files} and fetch_step={fetch_step} settings")

        test_data = SimpleIterDataset(
            {name: filelist},
            args.data_config,
            for_training=False,
            load_range_and_fraction=(
                tuple(args.test_range),
                args.data_fraction,
                args.data_split_group,
            ),
            fetch_by_files=fetch_by_files,
            fetch_step=fetch_step,
            name="test_" + name,
        )
        _logger.info(f"Instantiating test dataloader for {name} with batch_size={args.batch_size}")
        test_loader = DataLoader(test_data, num_workers=num_workers, batch_size=args.batch_size, drop_last=False,
                                 pin_memory=True)
        return test_loader

    size_threshold = args.fetch_by_files_threshold
    test_loaders = {name: functools.partial(get_test_loader, name, size_threshold) for name in file_dict}
    data_config = SimpleIterDataset({}, args.data_config, for_training=False).config
    return test_loaders, data_config


def onnx(args, model, data_config, model_info):
    """
    Saving model as ONNX.
    :param args:
    :param model:
    :param data_config:
    :param model_info:
    :return:
    """
    assert (args.export_onnx.endswith('.onnx'))
    model_path = args.model_prefix
    _logger.info('Exporting model %s to ONNX' % model_path)
    model.load_state_dict(torch.load(model_path, map_location='cpu'))
    model = model.cpu()
    model.eval()

    os.makedirs(os.path.dirname(args.export_onnx), exist_ok=True)
    inputs = tuple(
        torch.ones(model_info['input_shapes'][k], dtype=torch.float32) for k in model_info['input_names'])
    torch.onnx.export(model, inputs, args.export_onnx,
                      input_names=model_info['input_names'],
                      output_names=model_info['output_names'],
                      dynamic_axes=model_info.get('dynamic_axes', None),
                      opset_version=14) # 11 for 10_6, 14 for Run 3
    _logger.info('ONNX model saved to %s', args.export_onnx)

    preprocessing_json = os.path.join(os.path.dirname(args.export_onnx), 'preprocess.json')
    data_config.export_json(preprocessing_json)
    _logger.info('Preprocessing parameters saved to %s', preprocessing_json)


def flops(model, model_info):
    """
    Count FLOPs and params.
    :param args:
    :param model:
    :param model_info:
    :return:
    """
    from utils.flops_counter import get_model_complexity_info
    import copy

    model = copy.deepcopy(model).cpu()
    model.eval()

    _logger.info(f"Counting FLOPs and number of parameters for model {model.__class__.__name__}")
    _logger.debug(f"Model info: {model_info}")
    inputs = tuple(
        torch.ones(model_info["input_shapes"][k], dtype=torch.float32)
        for k in model_info["input_names"]
    )

    macs, params = get_model_complexity_info(model, inputs, as_strings=True, print_per_layer_stat=True, verbose=True)
    _logger.info('{:<30}  {:<8}'.format('Computational complexity: ', macs))
    _logger.info('{:<30}  {:<8}'.format('Number of parameters: ', params))


def profile(args, model, model_info, device):
    """
    Profile.
    :param model:
    :param model_info:
    :return:
    """
    import copy
    from torch.profiler import profile, record_function, ProfilerActivity

    model = copy.deepcopy(model)
    model = model.to(device)
    model.eval()

    inputs = tuple(
        torch.ones((args.batch_size,) + model_info['input_shapes'][k][1:],
                   dtype=torch.float32).to(device) for k in model_info['input_names'])
    for x in inputs:
        print(x.shape, x.device)

    def trace_handler(p):
        output = p.key_averages().table(sort_by="self_cuda_time_total", row_limit=50)
        print(output)
        p.export_chrome_trace("/tmp/trace_" + str(p.step_num) + ".json")

    with profile(
        activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
        schedule=torch.profiler.schedule(
            wait=2,
            warmup=2,
            active=6,
            repeat=2),
        on_trace_ready=trace_handler
    ) as p:
        for idx in range(100):
            model(*inputs)
            p.step()


def optim(args, model, device):
    """
    Optimizer and scheduler.
    :param args:
    :param model:
    :return:
    """
    optimizer_options = {k: ast.literal_eval(v) for k, v in args.optimizer_option}
    _logger.info('Optimizer options: %s' % str(optimizer_options))

    names_lr_mult = []
    if 'weight_decay' in optimizer_options or 'lr_mult' in optimizer_options:
        # https://github.com/rwightman/pytorch-image-models/blob/master/timm/optim/optim_factory.py#L31
        import re
        decay, no_decay = {}, {}
        names_no_decay = []
        for name, param in model.named_parameters():
            if not param.requires_grad:
                continue  # frozen weights
            if len(param.shape) == 1 or name.endswith(".bias") or (
                    hasattr(model, 'no_weight_decay') and name in model.no_weight_decay()):
                no_decay[name] = param
                names_no_decay.append(name)
            else:
                decay[name] = param

        decay_1x, no_decay_1x = [], []
        decay_mult, no_decay_mult = [], []
        mult_factor = 1
        if 'lr_mult' in optimizer_options:
            pattern, mult_factor = optimizer_options.pop('lr_mult')
            for name, param in decay.items():
                if re.match(pattern, name):
                    decay_mult.append(param)
                    names_lr_mult.append(name)
                else:
                    decay_1x.append(param)
            for name, param in no_decay.items():
                if re.match(pattern, name):
                    no_decay_mult.append(param)
                    names_lr_mult.append(name)
                else:
                    no_decay_1x.append(param)
            assert(len(decay_1x) + len(decay_mult) == len(decay))
            assert(len(no_decay_1x) + len(no_decay_mult) == len(no_decay))
        else:
            decay_1x, no_decay_1x = list(decay.values()), list(no_decay.values())
        wd = optimizer_options.pop('weight_decay', 0.)
        parameters = [
            {'params': no_decay_1x, 'weight_decay': 0.},
            {'params': decay_1x, 'weight_decay': wd},
            {'params': no_decay_mult, 'weight_decay': 0., 'lr': args.start_lr * mult_factor},
            {'params': decay_mult, 'weight_decay': wd, 'lr': args.start_lr * mult_factor},
        ]
        _logger.info('Parameters excluded from weight decay:\n - %s', '\n - '.join(names_no_decay))
        if len(names_lr_mult):
            _logger.info('Parameters with lr multiplied by %s:\n - %s', mult_factor, '\n - '.join(names_lr_mult))
    elif 'freeze' in optimizer_options:
        pattern = optimizer_options.pop('freeze')
        import re
        for name, param in model.named_parameters():
            if not param.requires_grad:
                continue
            if re.match(pattern, name):
                param.requires_grad = False
                _logger.info('Freeze parameter %s' % name)
        parameters = filter(lambda p: p.requires_grad, model.parameters())

    else:
        parameters = model.parameters()

    if args.optimizer == 'ranger':
        from utils.nn.optimizer.ranger import Ranger
        opt = Ranger(parameters, lr=args.start_lr, **optimizer_options)
    elif args.optimizer == 'adam':
        opt = torch.optim.Adam(parameters, lr=args.start_lr, **optimizer_options)
    elif args.optimizer == 'adamW':
        opt = torch.optim.AdamW(parameters, lr=args.start_lr, **optimizer_options)
    elif args.optimizer == 'radam':
        opt = torch.optim.RAdam(parameters, lr=args.start_lr, **optimizer_options)
    elif args.optimizer == 'sgd':
        opt = torch.optim.SGD(parameters, lr=args.start_lr, **optimizer_options)

    # load previous training and resume if `--load-epoch` is set
    if args.load_epoch is not None:
        _logger.info('Resume training from epoch %d' % args.load_epoch)
        model_state = torch.load(args.model_prefix + '_epoch-%d_state.pt' % args.load_epoch, map_location=device)
        if isinstance(model, torch.nn.parallel.DistributedDataParallel):
            model.module.load_state_dict(model_state)
        else:
            model.load_state_dict(model_state)
        opt_state_file = args.model_prefix + '_epoch-%d_optimizer.pt' % args.load_epoch
        if os.path.exists(opt_state_file):
            opt_state = torch.load(opt_state_file, map_location=device)
            opt.load_state_dict(opt_state)
        else:
            _logger.warning('Optimizer state file %s NOT found!' % opt_state_file)

    scheduler = None
    if args.lr_finder is None:
        if args.lr_scheduler == 'steps':
            lr_step = round(args.num_epochs / 3)
            scheduler = torch.optim.lr_scheduler.MultiStepLR(
                opt, milestones=[lr_step, 2 * lr_step], gamma=0.1,
                last_epoch=-1 if args.load_epoch is None else args.load_epoch)
        elif args.lr_scheduler == 'flat+decay':
            num_decay_epochs = max(1, int(args.num_epochs * 0.3))
            milestones = list(range(args.num_epochs - num_decay_epochs, args.num_epochs))
            gamma = 0.01 ** (1. / num_decay_epochs)
            if len(names_lr_mult):
                def get_lr(epoch): return gamma ** max(0, epoch - milestones[0] + 1)  # noqa
                scheduler = torch.optim.lr_scheduler.LambdaLR(
                    opt, (lambda _: 1, lambda _: 1, get_lr, get_lr),
                    last_epoch=-1 if args.load_epoch is None else args.load_epoch, verbose=True)
            else:
                scheduler = torch.optim.lr_scheduler.MultiStepLR(
                    opt, milestones=milestones, gamma=gamma,
                    last_epoch=-1 if args.load_epoch is None else args.load_epoch)
        elif args.lr_scheduler == 'flat+linear' or args.lr_scheduler == 'flat+cos':
            total_steps = args.num_epochs * args.steps_per_epoch
            warmup_steps = args.warmup_steps
            flat_steps = total_steps * 0.7 - 1
            min_factor = 0.001

            def lr_fn(step_num):
                if step_num > total_steps:
                    raise ValueError(
                        "Tried to step {} times. The specified number of total steps is {}".format(
                            step_num + 1, total_steps))
                if step_num < warmup_steps:
                    return 1. * step_num / warmup_steps
                if step_num <= flat_steps:
                    return 1.0
                pct = (step_num - flat_steps) / (total_steps - flat_steps)
                if args.lr_scheduler == 'flat+linear':
                    return max(min_factor, 1 - pct)
                else:
                    return max(min_factor, 0.5 * (math.cos(math.pi * pct) + 1))

            scheduler = torch.optim.lr_scheduler.LambdaLR(
                opt, lr_fn, last_epoch=-1 if args.load_epoch is None else args.load_epoch * args.steps_per_epoch)
            scheduler._update_per_step = True  # mark it to update the lr every step, instead of every epoch
        elif args.lr_scheduler == 'one-cycle':
            scheduler = torch.optim.lr_scheduler.OneCycleLR(
                opt, max_lr=args.start_lr, epochs=args.num_epochs, steps_per_epoch=args.steps_per_epoch, pct_start=0.3,
                anneal_strategy='cos', div_factor=25.0, last_epoch=-1 if args.load_epoch is None else args.load_epoch)
            scheduler._update_per_step = True  # mark it to update the lr every step, instead of every epoch
        elif args.lr_scheduler == 'cosanneal':
            base_scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(opt, 4, 2, verbose=False)
            from utils.nn.scheduler.warmup import GradualWarmupScheduler
            scheduler = GradualWarmupScheduler(opt, multiplier=1,
                                                warmup_epoch=5,
                                                after_scheduler=base_scheduler) ## warmup
    return opt, scheduler


def model_setup(args, data_config):
    """
    Loads the model
    :param args:
    :param data_config:
    :return: model, model_info, network_module, network_options
    """
    network_module = import_module(args.network_config, name='_network_module')
    network_options = {k: ast.literal_eval(v) for k, v in args.network_option}
    _logger.info('Network options: %s' % str(network_options))
    if args.export_onnx:
        network_options['for_inference'] = True
    if args.use_amp:
        network_options['use_amp'] = True
    model, model_info = network_module.get_model(data_config, **network_options)
    if args.compile_model:
        model = torch.compile(model)
    if args.load_model_weights:
        if args.load_model_weights == 'finetune_gghww_custom':
            model_state = torch.load("/home/olympus/licq/hww/incl-train/weaver-core/weaver/model/ak8_MD_vminclv2ParT_manual_fixwrap/net_best_epoch_state.pt", map_location='cpu')
            state_dict = model.state_dict()
            state_dict['mlp.0.weight'].copy_(model_state['part.fc.0.weight'][-1:].data)
            state_dict['mlp.0.bias'].copy_(model_state['part.fc.0.bias'][-1:].data)
        if args.load_model_weights == 'finetune_stage2_AD':
            model_state = torch.load("/home/olympus/licq/hww/incl-train/weaver-core/weaver/model/ak8_MD_inclv8_part_addltphp_wmeasonly_manual.useamp.large.gm5.ddp-bs256-lr2e-3/net_best_epoch_state.pt", map_location='cpu')
            state_dict = model.state_dict()
            state_dict['ft_mlp.0.0.weight'].copy_(model_state['part.fc.0.0.weight'][-1:].data)
            state_dict['ft_mlp.0.0.bias'].copy_(model_state['part.fc.0.0.bias'][-1:].data)
        if args.load_model_weights.startswith('finetune_stage2.'):
            # model_state = torch.load("/home/olympus/licq/hww/incl-train/weaver-core/weaver/model/ak8_MD_inclv8_part_addltphp_wmeasonly_manual.useamp.large.gm5.ddp-bs256-lr2e-3/net_best_epoch_state.pt", map_location='cpu')
            model_state = torch.load("/hwwtaggervol/hh4b/weights/glopartv2_base.pt")
            state_dict = model.state_dict()
            print(f"{state_dict.keys()=}")
            if args.load_model_weights == 'finetune_stage2.0': # only takes the params 0-th layer after ft layer
                state_dict[f'part.fc.0.0.weight'].copy_(model_state[f'part.fc.0.0.weight'].data)
                state_dict[f'part.fc.0.0.bias'].copy_(model_state[f'part.fc.0.0.bias'].data)
            elif args.load_model_weights == 'finetune_stage2.all': # take all layers after ft nodes 
                state_dict[f'part.fc.0.0.weight'].copy_(model_state[f'part.fc.0.0.weight'].data)
                state_dict[f'part.fc.0.0.bias'].copy_(model_state[f'part.fc.0.0.bias'].data)
                state_dict[f'part.fc.1.0.weight'].copy_(model_state[f'part.fc.1.weight'].data)
                state_dict[f'part.fc.1.0.bias'].copy_(model_state[f'part.fc.1.bias'].data)
            elif args.load_model_weights == 'finetune_stage2.exactcopy.higgs+qcd':
                state_dict[f'part.fc.0.0.weight'].copy_(model_state[f'part.fc.0.0.weight'].data)
                state_dict[f'part.fc.0.0.bias'].copy_(model_state[f'part.fc.0.0.bias'].data)
                state_dict[f'part.fc.1.weight'].copy_(model_state[f'part.fc.1.weight'].data[[17,18,19,20,21,22,23,24,25,26,27,28,29,309,310,311,312,313]])
                state_dict[f'part.fc.1.bias'].copy_(model_state[f'part.fc.1.bias'].data[[17,18,19,20,21,22,23,24,25,26,27,28,29,309,310,311,312,313]])
            elif args.load_model_weights == 'finetune_stage2.exactcopy.res_mass':
                state_dict[f'part.fc.0.0.weight'].copy_(model_state[f'part.fc.0.0.weight'].data)
                state_dict[f'part.fc.0.0.bias'].copy_(model_state[f'part.fc.0.0.bias'].data)
                state_dict[f'part.fc.1.weight'].copy_(model_state[f'part.fc.1.weight'].data[[314]])
                state_dict[f'part.fc.1.bias'].copy_(model_state[f'part.fc.1.bias'].data[[314]])
        elif args.load_model_weights.startswith('finetune_pheno.'):
            model_state = torch.load(os.path.expanduser("~/hww/incl-train/weaver-core/weaver/model/JetClassII_ak8puppi_full_scale/net_best_epoch_state.pt"), map_location='cpu')
            state_dict = model.state_dict()
            print(state_dict.keys())
            if args.load_model_weights == 'finetune_pheno.all': # take all layers after ft nodes
                state_dict[f'ft_mlp.0.0.weight'].copy_(model_state[f'mod.fc.0.0.weight'].data)
                state_dict[f'ft_mlp.0.0.bias'].copy_(model_state[f'mod.fc.0.0.bias'].data)
                state_dict[f'ft_mlp.1.0.weight'].copy_(model_state[f'mod.fc.1.weight'].data)
                state_dict[f'ft_mlp.1.0.bias'].copy_(model_state[f'mod.fc.1.bias'].data)
            elif args.load_model_weights == 'finetune_pheno.0': # only takes the params 0-th layer after ft layer
                state_dict[f'ft_mlp.0.0.weight'].copy_(model_state[f'mod.fc.0.0.weight'].data)
                state_dict[f'ft_mlp.0.0.bias'].copy_(model_state[f'mod.fc.0.0.bias'].data)
            elif (args.load_model_weights.startswith('finetune_pheno.ensemble.0') or args.load_model_weights.startswith('finetune_pheno.ensemble.all')) and 'wgtloss' not in args.load_model_weights: # for MLP ensembles
                if args.load_model_weights.startswith('finetune_pheno.ensemble.all'):
                    copy_targets = [
                        ('mod.fc.0.0.weight', 'ft_mlp.0.0.weight'),
                        ('mod.fc.0.0.bias', 'ft_mlp.0.0.bias'),
                        ('mod.fc.1.weight', 'ft_mlp.1.0.weight'),
                        ('mod.fc.1.bias', 'ft_mlp.1.0.bias'),
                    ]
                elif args.load_model_weights.startswith('finetune_pheno.ensemble.0'):
                    copy_targets = [
                        ('mod.fc.0.0.weight', 'ft_mlp.0.0.weight'),
                        ('mod.fc.0.0.bias', 'ft_mlp.0.0.bias'),
                    ]
                for key in state_dict.keys():
                    for mother_key, dau_match_key in copy_targets:
                        if key.endswith(dau_match_key):
                            state_dict[key].copy_(model_state[mother_key].data)
                            print(f'Copy {mother_key} -> {key}')
                if '+part' in args.load_model_weights:
                    for key in model_state.keys():
                        state_dict['part.'+key].copy_(model_state[key].data)
                        print(f'Copy part model params: {key}')
            elif args.load_model_weights.startswith('finetune_pheno.ensemble.0+wgtloss:'):
                weight_model_path = args.load_model_weights.split(':')[-1]
                weight_model_state = torch.load(weight_model_path, map_location='cpu')
                copy_targets = [
                    ('mod.fc.0.0.weight', 'ft_mlp.0.0.weight'),
                    ('mod.fc.0.0.bias', 'ft_mlp.0.0.bias'),
                ]
                for key in state_dict.keys():
                    if key.startswith('m_weight.'):
                        state_dict[key].copy_(weight_model_state[key.replace('m_weight.', '')].data)
                        print(f'Copy weight model params: {key}')
                    elif key.startswith('m_main.'):
                        for mother_key, dau_match_key in copy_targets:
                            if key.endswith(dau_match_key):
                                state_dict[key].copy_(model_state[mother_key].data)
                                print(f'Copy main model params: {mother_key} -> {key}')
                    else:
                        raise ValueError(f'Unknown key: {key}')

        elif args.load_model_weights.startswith('finetune_pheno_mergeQCD.'):
            model_state = torch.load("/home/olympus/licq/hww/incl-train/weaver-core/weaver/model/JetClassII_ak8puppi_full_scale_mergeQCD/net_best_epoch_state.pt", map_location='cpu')
            state_dict = model.state_dict()
            if args.load_model_weights == 'finetune_pheno_mergeQCD.all': # take all layers after ft nodes
                state_dict[f'ft_mlp.0.0.weight'].copy_(model_state[f'mod.fc.0.0.weight'].data)
                state_dict[f'ft_mlp.0.0.bias'].copy_(model_state[f'mod.fc.0.0.bias'].data)
                state_dict[f'ft_mlp.1.0.weight'].copy_(model_state[f'mod.fc.1.weight'].data)
                state_dict[f'ft_mlp.1.0.bias'].copy_(model_state[f'mod.fc.1.bias'].data)
        elif args.load_model_weights.startswith('wgtloss:'):
            state_dict = model.state_dict()
            weight_model_path = args.load_model_weights.split(':')[-1]
            weight_model_state = torch.load(weight_model_path, map_location='cpu')
            for key in state_dict.keys():
                if key.startswith('m_weight.'):
                    state_dict[key].copy_(weight_model_state[key.replace('m_weight.', '')].data)
                    print(f'Copy weight model params: {key}')

        else:
            model_state = torch.load(args.load_model_weights, map_location='cpu')
            missing_keys, unexpected_keys = model.load_state_dict(model_state, strict=False)
            _logger.info('Model initialized with weights from %s\n ... Missing: %s\n ... Unexpected: %s' %
                        (args.load_model_weights, missing_keys, unexpected_keys))
    # _logger.info(model)
    flops(model, model_info)
    # loss function
    try:
        loss_func = network_module.get_loss(data_config, **network_options)
        _logger.info('Using loss function %s with options %s' % (loss_func, network_options))
    except AttributeError:
        loss_func = torch.nn.CrossEntropyLoss()
        _logger.warning('Loss function not defined in %s. Will use `torch.nn.CrossEntropyLoss()` by default.',
                        args.network_config)
    return model, model_info, loss_func


def iotest(args, data_loader):
    """
    Io test
    :param args:
    :param data_loader:
    :return:
    """
    from tqdm.auto import tqdm
    from collections import defaultdict
    from utils.data.tools import _concat
    _logger.info('Start running IO test')
    monitor_info = defaultdict(list)

    for X, y, Z in tqdm(data_loader):
        for k, v in Z.items():
            monitor_info[k].append(v.cpu().numpy())
    monitor_info = {k: _concat(v) for k, v in monitor_info.items()}
    if monitor_info:
        monitor_output_path = 'weaver_monitor_info.pkl'
        import pickle
        with open(monitor_output_path, 'wb') as f:
            pickle.dump(monitor_info, f)
        _logger.info('Monitor info written to %s' % monitor_output_path)


def save_root(args, output_path, data_config, scores, labels, observers):
    """
    Saves as .root
    :param data_config:
    :param scores:
    :param labels
    :param observers
    :return:
    """
    from utils.data.fileio import _write_root
    output = {}
    scores_cls, scores_reg = (scores, None) if args.train_mode == 'cls' else (None, scores) if args.train_mode == 'regression' else scores
    # write regression nodes
    if args.train_mode == 'regression':
        name = data_config.label_names[0]
        output[name] = labels[name]
        output['output_' + name] = scores_reg
    if args.train_mode == 'hybrid':
        for idx in range(1, len(data_config.label_names)):
            name = data_config.label_names[idx]
            output[name] = labels[name]
            if not data_config.split_per_cls:
                output['output_' + name] = scores_reg[:, idx-1]
            else:
                for idx_cls, label_name in enumerate(data_config.label_value_cls_names):
                    output['output_' + name + '_' + label_name] = scores_reg[:, (idx-1) * data_config.label_value_cls_num + idx_cls]
    # write classification nodes
    if args.train_mode in ['cls', 'hybrid']:
        if data_config.label_value is not None:
            for idx, label_name in enumerate(data_config.label_value):
                output[label_name] = (labels['_label_'] == idx)
                output['score_' + label_name] = scores_cls[:, idx]
        else:
            output['cls_index'] = labels['_label_'] # classes can be too many, only store the index
            for idx, label_name in enumerate(data_config.label_value_cls_names):
                output['score_' + label_name] = scores_cls[:, idx]
    for k, v in labels.items():
        if k == data_config.label_names[0]:
            continue
        if v.ndim > 1:
            _logger.warning('Ignoring %s, not a 1d array.', k)
            continue
        output[k] = v
    for k, v in observers.items():
        if v.ndim > 1:
            _logger.warning('Ignoring %s, not a 1d array.', k)
            continue
        output[k] = v
    _write_root(output_path, output)


def save_parquet(args, output_path, scores, labels, observers):
    """
    Saves as parquet file
    :param scores:
    :param labels:
    :param observers:
    :return:
    """
    import awkward as ak
    output = {'scores': scores}
    output.update(labels)
    output.update(observers)
    ak.to_parquet(ak.Array(output), output_path, compression='LZ4', compression_level=4)

def _main(args):
    _logger.info('args:\n - %s', '\n - '.join(str(it) for it in args.__dict__.items()))

    if args.file_fraction < 1:
        _logger.warning('Use of `file-fraction` is not recommended in general -- prefer using `data-fraction` instead.')

    # classification/regression mode
    if args.train_mode == 'regression':
        _logger.info('Running in regression mode')
        from utils.nn.tools import train_regression as train
        from utils.nn.tools import evaluate_regression as evaluate
    elif args.train_mode == 'hybrid':
        _logger.info('Running in hybrid mode')
        from utils.nn.tools import train_hybrid as train
        from utils.nn.tools import evaluate_hybrid as evaluate
    elif args.train_mode == 'custom':
        _logger.info('Running in customised mode')
        from utils.nn.tools import train_custom as train
        from utils.nn.tools import evaluate_custom as evaluate
    else:
        _logger.info('Running in classification mode')
        from utils.nn.tools import train_classification as train
        from utils.nn.tools import evaluate_classification as evaluate
        if args.train_mode_params == 'metric:loss':
            from functools import partial
            evaluate = partial(evaluate, best_val_metrics='loss')

    # training/testing mode
    training_mode = not args.predict

    # device
    if args.gpus:
        # distributed training
        if args.backend is not None:
            local_rank = args.local_rank
            torch.cuda.set_device(local_rank)
            gpus = [local_rank]
            dev = torch.device(local_rank)
            import datetime
            torch.distributed.init_process_group(backend=args.backend, timeout=datetime.timedelta(seconds=5400))
            _logger.info(f'Using distributed PyTorch with {args.backend} backend')
        else:
            gpus = [int(i) for i in args.gpus.split(',')]
            dev = torch.device(gpus[0])
    else:
        gpus = None
        dev = torch.device('cpu')

    # torch configs
    if torch.__version__.startswith('2.'):
        torch.set_float32_matmul_precision('high')

    # load data
    if training_mode:
        train_loader, val_loader, data_config, train_input_names, train_label_names = train_load(args)
    else:
        test_loaders, data_config = test_load(args)

    if args.io_test:
        data_loader = train_loader if training_mode else list(test_loaders.values())[0]()
        iotest(args, data_loader)
        return

    model, model_info, loss_func = model_setup(args, data_config)

    # Detect NaN        
    def hook_anomaly(module, input, output):
        def check_tensor(x, tensor_name):
            if isinstance(x, torch.Tensor):
                if torch.isnan(x).any():
                    raise RuntimeError(f"NaN detected in {module.__class__.__name__}, {tensor_name}")
                if torch.isinf(x).any():
                    raise RuntimeError(f"Inf detected in {module.__class__.__name__}, {tensor_name}")

        def check_structure(x, name):
            if isinstance(x, torch.Tensor):
                check_tensor(x, name)
            elif isinstance(x, tuple) or isinstance(x, list):
                for i, item in enumerate(x):
                    check_structure(item, f"{name}[{i}]")
            elif isinstance(x, dict):
                for k, v in x.items():
                    check_structure(v, f"{name}['{k}']")

        check_structure(input, "input")
        check_structure(output, "output")

    for name, module in model.named_modules():
        module.register_forward_hook(hook_anomaly)

    # TODO: load checkpoint
    # if args.backend is not None:
    #     load_checkpoint()

    if args.print:
        return

    if args.profile:
        profile(args, model, model_info, device=dev)
        return

    # export to ONNX
    if args.export_onnx:
        onnx(args, model, data_config, model_info)
        return

    if args.tensorboard:
        from utils.nn.tools import TensorboardHelper
        tb = TensorboardHelper(tb_comment=args.tensorboard, tb_custom_fn=args.tensorboard_custom_fn)
    else:
        tb = None

    # note: we should always save/load the state_dict of the original model, not the one wrapped by nn.DataParallel
    # so we do not convert it to nn.DataParallel now
    orig_model = model

    if training_mode:
        model = orig_model.to(dev)

        # DistributedDataParallel
        if args.backend is not None:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=gpus, output_device=local_rank)

        # optimizer & learning rate
        opt, scheduler = optim(args, model, dev)

        # DataParallel
        if args.backend is None:
            if gpus is not None and len(gpus) > 1:
                # model becomes `torch.nn.DataParallel` w/ model.module being the original `torch.nn.Module`
                model = torch.nn.DataParallel(model, device_ids=gpus)
            # model = model.to(dev)

        # lr finder: keep it after all other setups
        if args.lr_finder is not None:
            start_lr, end_lr, num_iter = args.lr_finder.replace(' ', '').split(',')
            from utils.lr_finder import LRFinder
            lr_finder = LRFinder(model, opt, loss_func, device=dev, input_names=train_input_names,
                                 label_names=train_label_names)
            lr_finder.range_test(train_loader, start_lr=float(start_lr), end_lr=float(end_lr), num_iter=int(num_iter))
            lr_finder.plot(output='lr_finder.png')  # to inspect the loss-learning rate graph
            return

        # training loop
        best_valid_metric = np.inf if args.train_mode in ['regression', 'hybrid'] or (args.train_mode == 'cls' and args.train_mode_params == 'metric:loss') else 0
        grad_scaler = torch.cuda.amp.GradScaler() if args.use_amp else None
        for epoch in range(args.num_epochs):
            if args.load_epoch is not None:
                if epoch <= args.load_epoch:
                    continue
            _logger.info('-' * 50)

            if args.run_mode in ['default', 'train-only']:
                _logger.info('Epoch #%d training' % epoch)
                train_loss = train(model, loss_func, opt, scheduler, train_loader, dev, epoch,
                    steps_per_epoch=args.steps_per_epoch, grad_scaler=grad_scaler, tb_helper=tb)
                # train_loader.dataset.restart_at_curr_pos()
                if args.model_prefix and (args.backend is None or local_rank == 0):
                    dirname = os.path.dirname(args.model_prefix)
                    if dirname and not os.path.exists(dirname):
                        os.makedirs(dirname)
                    state_dict = model.module.state_dict() if isinstance(
                        model, (torch.nn.DataParallel, torch.nn.parallel.DistributedDataParallel)) else model.state_dict()
                    torch.save(state_dict, args.model_prefix + '_epoch-%d_state.pt' % epoch)
                    torch.save(opt.state_dict(), args.model_prefix + '_epoch-%d_optimizer.pt' % epoch)
                # if args.backend is not None and local_rank == 0:
                # TODO: save checkpoint
                #     save_checkpoint()

            if args.run_mode in ['default', 'val-only']:
                _logger.info('Epoch #%d validating' % epoch)
                if args.run_mode == 'val-only':
                    # check if the model to load exists
                    import time
                    while not os.path.exists(args.model_prefix + '_epoch-%d_state.pt' % epoch):
                        _logger.info('Waiting for model %s to be ready...' % (args.model_prefix + '_epoch-%d_state.pt' % epoch))
                        time.sleep(10)
                    time.sleep(10)
                    try:
                        model.load_state_dict(torch.load(args.model_prefix + '_epoch-%d_state.pt' % epoch, map_location=dev))
                    except RuntimeError:
                        state_dict = torch.load(args.model_prefix + '_epoch-%d_state.pt' % epoch, map_location=dev)
                        if (args.gpus) and (args.backend is not None) and (not isinstance(model, torch.nn.parallel.DistributedDataParallel)):
                            _logger.info("Wrapping model in DistributedDataParallel")
                            model = torch.nn.parallel.DistributedDataParallel(
                                model, device_ids=gpus, output_device=local_rank
                            )
                            model.load_state_dict(state_dict)
                        else:
                            # If it's already wrapped and still fails, we need to modify the state dict
                            _logger.info("Modifying state dict keys")
                            new_state_dict = {"module." + k: v for k, v in state_dict.items() if not k.startswith("module.")}
                            model.load_state_dict(new_state_dict)
                valid_metric = evaluate(model, val_loader, dev, epoch, loss_func=loss_func,
                                        steps_per_epoch=args.steps_per_epoch_val, tb_helper=tb)
                # val_loader.dataset.restart_at_curr_pos()
                is_best_epoch = (
                    valid_metric < best_valid_metric) if args.train_mode in ['regression', 'hybrid'] or (args.train_mode == 'cls' and args.train_mode_params == 'metric:loss') else(
                    valid_metric > best_valid_metric)
                if is_best_epoch:
                    best_valid_metric = valid_metric
                    if args.model_prefix and (args.backend is None or local_rank == 0):
                        shutil.copy2(args.model_prefix + '_epoch-%d_state.pt' %
                                    epoch, args.model_prefix + '_best_epoch_state.pt')
                        # torch.save(model, args.model_prefix + '_best_epoch_full.pt')
                _logger.info('Epoch #%d: Current validation metric: %.5f (best: %.5f)' %
                            (epoch, valid_metric, best_valid_metric), color='bold')

            if args.early_stop:
                assert args.run_mode == 'default'
                # override the best_epoch behavier and break if the eval loss exceeds the training loss too much
                if args.train_mode == 'cls':
                    assert args.train_mode_params == 'metric:loss'
                if args.model_prefix and (args.backend is None or local_rank == 0):
                    shutil.copy2(args.model_prefix + '_epoch-%d_state.pt' %
                                epoch, args.model_prefix + '_best_epoch_state.pt')
                if valid_metric - train_loss > args.early_stop_dlr:
                    _logger.info('Early stop at epoch %d' % epoch)
                    break

            if args.use_last_model:
                if args.model_prefix and (args.backend is None or local_rank == 0):
                    shutil.copy2(args.model_prefix + '_epoch-%d_state.pt' %
                                epoch, args.model_prefix + '_best_epoch_state.pt')

    if args.data_test:
        if args.backend is not None and local_rank != 0:
            return
        if training_mode:
            del train_loader, val_loader
            test_loaders, data_config = test_load(args)

        if not args.model_prefix.endswith('.onnx'):
            if args.predict_gpus:
                gpus = [int(i) for i in args.predict_gpus.split(',')]
                dev = torch.device(gpus[0])
            else:
                gpus = None
                dev = torch.device('cpu')
            model = orig_model.to(dev)
            model_path = args.model_prefix if args.model_prefix.endswith(
                '.pt') else args.model_prefix + '_best_epoch_state.pt'
            _logger.info('Loading model %s for eval' % model_path)
            try:
                model.load_state_dict(torch.load(model_path, map_location=dev))
            except FileNotFoundError as e:
                model_dir = os.path.dirname(model_path)
                # check if directory exists
                if not os.path.exists(model_dir):
                    error_msg = 'Model directory not found: %s' % model_dir
                    raise FileNotFoundError(error_msg)
                else:
                    # list all files in the directory
                    error_msg = "Model file not found: %s\n" % model_path
                    error_msg += "Files in the directory: %s:\n" % model_dir
                    error_msg += '\n'.join(os.listdir(model_dir))
                    _logger.error(error_msg)
                    raise e
            if gpus is not None and len(gpus) > 1:
                model = torch.nn.DataParallel(model, device_ids=gpus)
            model = model.to(dev)

        for name, get_test_loader in test_loaders.items():
            _logger.info(f'Getting test loader for {name}')
            test_loader = get_test_loader()
            _logger.info(f'Loaded test loader for {name}')
            # run prediction
            if args.model_prefix.endswith('.onnx'):
                _logger.info('Loading model %s for eval' % args.model_prefix)
                from utils.nn.tools import evaluate_onnx
                test_metric, scores, labels, observers = evaluate_onnx(args.model_prefix, test_loader)
            else:
                _logger.info(f'Running evaluation on {name}')
                test_metric, scores, labels, observers = evaluate(
                    model, test_loader, dev, epoch=None, for_training=False, tb_helper=tb)
            _logger.info('Test metric %.5f' % test_metric, color='bold')
            del test_loader

            if args.predict_output:
                if '/' not in args.predict_output:
                    args.predict_output = os.path.join(
                        os.path.dirname(args.model_prefix),
                        'predict_output', args.predict_output)
                os.makedirs(os.path.dirname(args.predict_output), exist_ok=True)
                if name == '':
                    output_path = args.predict_output
                else:
                    base, ext = os.path.splitext(args.predict_output)
                    output_path = base + '_' + name + ext
                if output_path.endswith('.root'):
                    save_root(args, output_path, data_config, scores, labels, observers)
                else:
                    save_parquet(args, output_path, scores, labels, observers)
                _logger.info('Written output to %s' % output_path, color='bold')


def main():
    args = parser.parse_args()

    if args.samples_per_epoch is not None:
        if args.steps_per_epoch is None:
            args.steps_per_epoch = args.samples_per_epoch // args.batch_size
        else:
            raise RuntimeError('Please use either `--steps-per-epoch` or `--samples-per-epoch`, but not both!')

    if args.samples_per_epoch_val is not None:
        if args.steps_per_epoch_val is None:
            args.steps_per_epoch_val = args.samples_per_epoch_val // args.batch_size
        else:
            raise RuntimeError('Please use either `--steps-per-epoch-val` or `--samples-per-epoch-val`, but not both!')

    if args.steps_per_epoch_val is None and args.steps_per_epoch is not None:
        args.steps_per_epoch_val = round(args.steps_per_epoch * (1 - args.train_val_split) / args.train_val_split)
    if args.steps_per_epoch_val is not None and args.steps_per_epoch_val < 0:
        args.steps_per_epoch_val = None

    if '{auto}' in args.model_prefix or '{auto}' in args.log_file:
        import hashlib
        import time
        model_name = time.strftime('%Y%m%d-%H%M%S') + "_" + os.path.basename(args.network_config).replace('.py', '')
        if len(args.network_option):
            model_name = model_name + "_" + hashlib.md5(str(args.network_option).encode('utf-8')).hexdigest()
        model_name += '_{optim}_lr{lr}_batch{batch}'.format(lr=args.start_lr,
                                                            optim=args.optimizer, batch=args.batch_size)
        args._auto_model_name = model_name
        args.model_prefix = args.model_prefix.replace('{auto}', model_name)
        args.log_file = args.log_file.replace('{auto}', model_name)
        if args.tensorboard is not None:
            args.tensorboard = args.tensorboard.replace('{auto}', model_name)
        print('Using auto-generated model prefix %s' % args.model_prefix)

    if args.predict_gpus is None:
        args.predict_gpus = args.gpus

    args.local_rank = None if args.backend is None else int(os.environ.get("LOCAL_RANK", "0"))

    stdout = sys.stdout
    if args.local_rank is not None:
        args.log_file += '.%03d' % args.local_rank
        if args.tensorboard is not None:
            args.tensorboard += '.%03d' % args.local_rank
        if args.local_rank != 0:
            stdout = None
    _configLogger('weaver', stdout=stdout, filename=args.log_file, loglevel=args.log_level)

    if args.seed != -1:
        _logger.info('Setting random seed to %d' % args.seed)
        torch.manual_seed(args.seed)
        np.random.seed(args.seed)

    _main(args)


if __name__ == '__main__':
    main()
