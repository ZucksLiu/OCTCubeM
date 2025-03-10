# Incorporated and modified from https://github.com/cozygene/SLIViT/blob/main/auxiliaries/misc.py

import os
import sys
import torch
import logging
import numpy as np
import pandas as pd
from torch.utils.data import DataLoader
from torch.utils.data import Subset, ConcatDataset
from sklearn.model_selection import GroupShuffleSplit



def assert_input_is_valid(args, slivit_logger):
    # Dictionary mapping dataset names to optional medmnist classes
    medmnist_classes = {'xray2d': 'ChestMNIST', 'ct3d': 'NoduleMNIST3D'}

    # Import the optional medmnist class if present
    args.medmnist_dataset = None
    if args.slivit_dataset in medmnist_classes:
        medmnist_class_name = medmnist_classes[args.slivit_dataset]
        medmnist_module = __import__('medmnist', fromlist=[medmnist_class_name])
        args.medmnist_dataset = getattr(medmnist_module, medmnist_class_name)
        print('args.medmnist_dataset:', args.medmnist_dataset)
        return  # no meta file, split, or labels should be checked

    # non-medmnist datasets
    # check labels
    for label in args.slivit_label:
        print(pd.read_csv(args.slivit_meta, nrows=0).columns)
        assert label in pd.read_csv(args.slivit_meta, nrows=0).columns, f'Label column {label} not found in the meta file.'

    check_split(args, slivit_logger)


def check_split(args, slivit_logger):
    meta = pd.read_csv(args.slivit_meta)
    args.script = get_script_name()
    print(meta.shape, args.script)

    if args.script == 'evaluate':
        assert args.slivit_split_col in meta.columns or \
               args.split_ratio[2] > 0 or \
               args.test_meta is not None, f'No test set was provided for evaluation (please provide either a ' \
                                           f'pre-defined split col, a positive test_ratio, or a --meta_test.'

    else:
        # pretrain or finetune
        if args.slivit_split_col in meta.columns:

            slivit_logger.info(f'Pre-defined split column was detected: {args.slivit_split_col}')
            assert meta[args.slivit_split_col].str.contains('train', case=False).any(), \
                "Pre-defined split does not contain a training set"
            assert meta[args.slivit_split_col].str.contains('val', case=False).any(), \
                "Pre-defined split does not contain a validation set"
            test_samples_in_split_col = meta[args.slivit_split_col].str.contains('test', case=False).any()
            if test_samples_in_split_col:
                slivit_logger.info(f'Pre-defined split column contains training, validation, and test sets.')
                assert args.slivit_test_meta is None, 'Ambiguous test set. Please provide either a split column or a test ' \
                                               'meta file, not both (a pre-defined split could be ignored "wrongly" ' \
                                               'setting --split_col).'
            else:
                logger.info(f'Pre-defined split column contains training and validation sets.')
                # args.test_meta could be None or a test meta file
            slivit_logger.info(f'Ignoring --split_ratio.')
            if 'slivit_split_ratio' not in args:
                args.slivit_split_ratio = None
        else:
            # no pre-defined split
            args.split_ratio = [0.7, 0.15, 0.15]
            assert sum(args.split_ratio) == 1, "Split ratios must sum to 1"
            train_ratio, val_ratio, test_ratio = args.split_ratio
            assert train_ratio > 0, "Training set ratio must be greater than 0"
            assert val_ratio > 0, "Validation set ratio must be greater than 0"
            assert val_ratio >= 0, "Test set ratio must be greater than or equal to 0"


def stringify_blr(blr):
    # Convert the float to scientific notation (e.g., 5e-3)
    return "{:.0e}".format(blr)

def init_out_dir(args):
    out_dir = args.slivit_out_dir.rstrip('/')
    print('out_dir:', out_dir)

    # by default, the csv file name (or dataset name in case of medmnist) is added to the output directory

    if args.slivit_meta is not None and args.slivit_dataset != 'ct3d': # goto csv file name add
        # example args.meta: ./meta/echonet.csv
        csv_file_name = os.path.splitext(args.slivit_meta.split("/")[-1])[0]  # remove extension
        out_dir = f'{out_dir}/{csv_file_name}' + (f'_{args.slivit_label[0]}' if len(args.slivit_label) == 1 else '')
        out_dir = out_dir +  '-' + args.regression_loss_name + '-lr-' + stringify_blr(args.blr)

    else:
        out_dir = f'{out_dir}/{args.slivit_dataset}'

    os.makedirs(out_dir, exist_ok=True)

    args.slivit_out_dir = out_dir.rstrip('/') # only useful
    print('args.out_dir:', out_dir) # ./outputs/us3d


def save_options(args, slivit_logger):
    arguments = []
    for arg in vars(args):
        value = getattr(args, arg)
        if isinstance(value, bool):
            if value:
                arguments.append(f'--{arg}')
        else:  # if value is not None:
            arguments.append(f'--{arg} "{value}"')
    if args.slivit_exp:
        script_name = get_script_name()
        command_file = f'{args.slivit_out_dir}/{script_name}_command.txt'
        with open(command_file, 'w') as f:
            print(sys.argv) # finetune.py
            f.write(' '.join(sys.argv) + "\n\n")

        # all options, including default values
        options_file = f'{args.slivit_out_dir}/{script_name}_options.txt'
        with open(options_file, 'w') as f:
            f.write(f"python {sys.argv[0]} {' '.join(arguments)}\n\n")

        slivit_logger.info(f'Running command is saved at:\n{command_file}\n')
        slivit_logger.info(f'Full running configuration (including default parameters) is saved at:\n{options_file}\n')


def get_predefined_split_indices(df, split_col, out_csv):
    # Get indices for train, val and test
    train_idx = np.argwhere(df[split_col].str.contains('train', case=False))
    val_idx = np.argwhere(df[split_col].str.contains('val', case=False))
    test_idx = np.argwhere(df[split_col].str.contains('test', case=False))
    df.to_csv(out_csv, index=False)
    return train_idx, val_idx, test_idx


def get_script_name():
    return sys.argv[0].split('/')[-1].split('.')[0]


def get_split_indices(meta, out_dir, split_ratio, pathology, split_col, pid_col):
    script_name = get_script_name()
    df = pd.read_csv(meta)
    if split_col in df.columns:
        train_idx, val_idx, test_idx = get_predefined_split_indices(df, split_col,
                                                                    f'{out_dir}/{os.path.split(meta)[-1]}')
    else:
        train_ratio, val_ratio, test_ratio = split_ratio
        if train_ratio + val_ratio == 0:
            logger.info('Using all samples for evaluation.')
            assert script_name == 'evaluate', 'Train and validation set should not be empty for training.'
            train_idx = np.array([])
            val_idx = np.array([])
            test_idx = np.arange(0, len(df))
        else:
            logger.info(f'Splitting the dataset according to the provided --split_ratio: {split_ratio}')
            gss = GroupShuffleSplit(n_splits=1, train_size=train_ratio)
            train_idx, temp_idx = next(gss.split(df,
                                                 y=None if script_name == 'pretrain' else df[pathology],
                                                 groups=df[pid_col]))  # split by patient
            # TODO: check if split "by file" for kermany
            # helps getting the high performance
            # groups=df['path']))  # split by patient

            if test_ratio == 0:
                # pre-training or fine-tuning
                # using external test set if exists
                logger.info('Split will include only train and validation sets (according to --split_ratio).')
                val_idx = temp_idx
                test_idx = temp_idx[[]]
            else:
                test_val_df = df.iloc[temp_idx]
                # Second split
                gss_temp = GroupShuffleSplit(n_splits=1, train_size=split_ratio[1] / sum(split_ratio[1:]))
                val_idx, test_idx = next(gss_temp.split(test_val_df,
                                                        y=test_val_df[pathology],
                                                        groups=test_val_df[pid_col]))  # split by patient

                # Map temporary indices back to original indices
                val_idx = temp_idx[val_idx]
                test_idx = temp_idx[test_idx]

            # Store split indices
            df[split_col] = 'train'
            df.loc[val_idx, split_col] = 'val'
            df.loc[test_idx, split_col] = 'test'
            df.to_csv(f'{out_dir}/{os.path.split(meta)[-1]}', index=False)

    return train_idx, val_idx, test_idx


def setup_dataloaders(args, slivit_logger):
    dataset_class = get_dataset_class(args.slivit_dataset)

    assert args.slivit_meta is not None or \
           args.medmnist_dataset is not None, \
        'Meta file is required for non-MedMNIST datasets. Please provide the meta file path.'
    print('Stop here 1', args.medmnist_dataset, args.slivit_meta, dataset_class)

    train_loader, valid_loader, test_loader = get_dataloaders(dataset_class, args, slivit_logger)

    dls = (train_loader, valid_loader)

    return dls, test_loader


def get_dataloaders(dataset_class, args, slivit_logger):
    msg = ''
    if args.medmnist_dataset is not None:
        # # TODO: make sure test returns empty when pretraining (use all samples for pretraining)
        # if args.medmnist_mocks:
        #     size = 28

        if 'xray' in args.slivit_dataset:
            # chestmnist
            size = 224
        else:
            # nodulemnist
            # size = 28  # TODO: change to 64 (and adjust the relevant transformation processing)
            size = 64
        print('data_root:', args.slivit_medmnist_root, args.medmnist_dataset)
        os.makedirs(args.slivit_medmnist_root, exist_ok=True)
        train_subset = dataset_class(args.medmnist_dataset(split="train", download=True, root=args.slivit_medmnist_root, size=size), num_slices_to_use=args.slivit_slices, use_3_channel=args.slivit_3_channels)
        valid_subset = dataset_class(args.medmnist_dataset(split="val", download=True, root=args.slivit_medmnist_root, size=size), num_slices_to_use=args.slivit_slices, use_3_channel=args.slivit_3_channels)
        test_subset = dataset_class(args.medmnist_dataset(split="test", download=True, root=args.slivit_medmnist_root, size=size), num_slices_to_use=args.slivit_slices, use_3_channel=args.slivit_3_channels)

        # if args.medmnist_mocks is not None:
            # msg += f'Running a mock version of the dataset with {args.medmnist_mocks} samples only!!'

        train_subset = Subset(train_subset,
                              np.arange(len(train_subset)))
        valid_subset = Subset(valid_subset,
                              np.arange(len(valid_subset)))
        test_subset = Subset(test_subset, np.arange(len(test_subset)))

        # dataset  = ConcatDataset([train_subset, valid_subset, test_subset])
        # us3d: 7465, 1288, 1277
    else:
        train_indices, valid_indices, test_indices = get_split_indices(args.slivit_meta, args.slivit_out_dir,
                                                                       args.slivit_split_ratio, args.slivit_label,
                                                                       args.slivit_split_col, args.slivit_pid_col)
        print('Stop here 2', len(train_indices), len(valid_indices), len(test_indices))
        print(dataset_class)
        if args.task_mode == 'regression':
            label_dtype = 'float'
        else:
            label_dtype = 'int'
        dataset = dataset_class(args.slivit_meta, args.slivit_label, args.slivit_path_col,
                                num_slices_to_use=args.slivit_slices,
                                sparsing_method=args.slivit_sparsing_method,
                                img_suffix=args.slivit_img_suffix,
                                label_dtype=label_dtype, get_auxi_reg=args.slivit_us_auxi_reg,
                                use_3_channel=args.slivit_3_channels, convert_to_vol=args.slivit_convert_vol)

        train_subset = Subset(dataset, train_indices)
        valid_subset = Subset(dataset, valid_indices)
        print('Stop here 3', len(train_subset), len(valid_subset), args.slivit_split_ratio)
        if len(test_indices) > 0:
            # internal test set
            msg = f'Using internal test set for final model evaluation'
            if args.slivit_split_ratio is None:
                msg += ' (split_ratio is not provided).'
            elif args.slivit_split_ratio[2] == 0:
                msg += ' (split_ratio 0 is overridden by a pre-defined split)'
            test_subset = Subset(dataset, test_indices)
        else:
            # external test set
            assert args.split_ratio[2] == 0, 'Test set is empty but split_ratio[2] is not 0'
            if args.test_meta is None:
                msg = 'No model evaluation will be done (test ratio was set to 0 and no test_meta was provided).'
                test_subset = Subset(dataset, [])  # empty test set
            else:
                msg = f'Using external test set for final model evaluation from:\n{args.test_meta}'
                test_df = pd.read_csv(args.test_meta)
                test_subset = Subset(dataset_class(test_df, args.label, args.path_col,
                                                   num_slices_to_use=args.slices,
                                                   sparsing_method=args.sparsing_method,
                                                   img_suffix=args.img_suffix),
                                     np.arange(len(test_df)))

    if msg and get_script_name() != 'evaluate':
        slivit_logger.info('\n\n' + '*' * 100 + f'\n{msg}\n' + '*' * 100 + '\n')

    # slivit_logger.info(f'Num of cpus is {args.cpus}')

    train_loader = DataLoader(train_subset, batch_size=args.batch_size, num_workers=args.num_workers, pin_memory=args.pin_mem, drop_last=True)
    slivit_logger.info(f'# of train batches is {len(train_loader)}, batch size is {args.batch_size}, dataset size is {len(train_subset)}') # 289, 4, 1158
    valid_loader = DataLoader(valid_subset, batch_size=args.batch_size, num_workers=args.num_workers, pin_memory=args.pin_mem, drop_last=False)
    slivit_logger.info(f'# of validation batches is {len(valid_loader)}, batch size is {args.batch_size}, dataset size is {len(valid_subset)}') # 41, 4, 165
    test_loader = DataLoader(test_subset, batch_size=args.batch_size, num_workers=args.num_workers, pin_memory=args.pin_mem, drop_last=False)
    slivit_logger.info(f'# of Test batches is {len(test_loader)} batch size is {args.batch_size}, dataset size is {len(test_subset)}\n') # 77, 4, 310
    return train_loader, valid_loader, test_loader


def get_dataset_class(dataset_name):
    # Dictionary mapping dataset names to dataset classes
    dataset_name_to_class_name = {'xray2d': 'MedMNISTDataset2D', 'oct2d': 'OCTDataset2D',
                                  'custom2d': 'CustomDataset2D',
                                  'oct3d': 'OCTDataset3D', 'us3d': 'USDataset3D',
                                  'mri3d': 'MRIDataset3D', 'ct3d': 'MedMNISTDataset3D',
                                  'custom3d': 'CustomDataset3D'}

    assert dataset_name in dataset_name_to_class_name, \
        f'Unknown dataset option. Please choose from: {list(dataset_name_to_class_name.keys())}'

    # Import the dataset class dynamically
    class_name = dataset_name_to_class_name[dataset_name]
    print(class_name)
    dataset_module = __import__(f'util.{class_name}', fromlist=[class_name])
    dataset_class = getattr(dataset_module, class_name)

    return dataset_class


def get_loss_and_metrics(task):
    if task == 'cls':
        loss_f = torch.nn.BCEWithLogitsLoss()  # TODO: consider using CrossEntropyLoss
        metrics = [RocAucMulti(), APScoreMulti()]
    elif task == 'reg':
        loss_f = torch.nn.L1Loss()
        metrics = [R2Score(), ExplainedVariance(), PearsonCorrCoef()]
    else:
        raise ValueError('Unknown task option')

    return loss_f, metrics


def create_learner(slivit, dls, args, model_dir):
    best_model_name = 'feature_extractor' if get_script_name() == 'pretrain' else 'slivit'
    loss_f, metrics = get_loss_and_metrics(args.task)
    learner = Learner(dls, slivit, model_dir=model_dir, loss_func=loss_f, metrics=metrics,
                      cbs=[SaveModelCallback(fname=best_model_name),
                           EarlyStoppingCallback(min_delta=args.min_delta, patience=args.patience),
                           CSVLogger(fname=logger.handlers[0].baseFilename, append=True)] +
                          ([WandbCallback()] if (args.wandb_name is not None and script_name != 'evaluate') else []))
    return learner, best_model_name


def train(args, learner, best_model_name):
    gpus = os.environ["CUDA_VISIBLE_DEVICES"].split(',')
    for gpu in range(len(gpus)):
        try:
            # Set the current GPU
            logger.info(f'Trying GPU {gpus[gpu]}')
            torch.cuda.set_device(gpu)  # Switch to the current GPU
            learner.model.to(f'cuda:{gpu}')  # Move model to the current GPU

            # Release previous GPU's memory if not on the first GPU
            if gpu > 0:
                torch.cuda.set_device(gpu - 1)  # Switch to the previous GPU
                torch.cuda.empty_cache()  # Release the memory of the previous GPU
                torch.cuda.set_device(gpu)  # Switch back to the current GPU

            # fit or fine-tune the model
            if args.finetune:
                learner.fine_tune(args.epochs, args.lr)
            else:
                # default
                learner.fit(args.epochs, args.lr)

            logger.info(f'Best model is stored at:\n{args.out_dir}/{best_model_name}.pth\n')

            # successful training
            return

        except RuntimeError as e:
            if 'out of memory' in e.args[0]:
                if gpu < len(gpus) - 1:
                    logger.error(f'GPU ran out of memory. Trying next GPU...\n')
                else:
                    # Handle failure case where all GPUs run out of memory or error out
                    logger.error('Out of memory error occurred on all GPUs.\n'
                                 'You may want to try reducing the batch size or using a larger GPU.'
                                 'Exiting...\n')
                    # Re-raise the exception for proper handling outside this function
                    raise e
            else:
                logger.error(f'Unrecognized error occurred: {e.args[0]}. Exiting...\n')
                raise e


def print_and_store_scores(learner, evaluation_loader, out_dir, preds=None):
    if preds is None:
        logger.info(f'Computing scores...')
        metric_scores = learner.validate(dl=evaluation_loader)

        logger.info('\n' + '*' * 100 + f'\nModel evaluation performance on test set is:')
    else:
        # TODO: implement score computation from predictions (instead of re-running inference)
        metric_scores = None

    metric_names = ['loss_score'] + [m.name for m in learner.metrics]  # loss is not included in the metrics
    for metric_score, metric_name in zip(metric_scores, metric_names):
        logger.info(f'{metric_name}: {metric_score:.5f}' + (('\n' + '*' * 100)
                                                            if metric_name == metric_names[-1] else ''))
        with open(f'{out_dir}/{metric_name}.txt', 'w') as f:
            f.write(f'{metric_score:.5f}\n')
    logger.info(f'Running result is saved at:\n{out_dir}')


def evaluate(learner, data_loader, weights_path, out_dir, meta, pid_col, path_col, split_col, label):
    # TODO: migrate this logic into evaluate_model() instead
    # Evaluate the model on the test set if provided
    if hasattr(data_loader, 'indices') and len(data_loader.indices) > 0 or \
            hasattr(data_loader, 'get_idxs') and len(data_loader.get_idxs()) > 0:
        learner.model.to('cuda')
        learner.load(weights_path.split('/')[-1].split('.pth')[0])
        preds = store_predictions(learner, data_loader, out_dir, meta, label, pid_col, path_col, split_col)
        print_and_store_scores(learner, data_loader, out_dir)
    else:
        # evaluation_loader is empty
        logger.info('Evaluation loader is empty. No evaluation is performed.')


def store_predictions(learner, test_loader, out_dir, meta, label, pid_col, path_col, split_col):
    logger.info(f'Computing predictions...')
    results_file = f'{out_dir}/predicted_scores.csv'
    preds = learner.get_preds(dl=test_loader)

    if args.medmnist_dataset:
        predictions = pd.DataFrame({'label': test_loader.dataset.dataset.dataset.labels.squeeze()[test_loader.indices.squeeze()]})
    else:
        df = pd.read_csv(meta).iloc[test_loader.indices.squeeze()]
        # double-check that the (original) true label in the meta file matches the true label in the data loader
        matches = np.isclose(preds[1].squeeze().numpy(), df[label].astype(float).values.squeeze())
        assert matches.all(), f'True label in meta does not match true label in data loader at indices {df.iloc[matches[~matches].index.to_list(), :]}'

        predictions = df[[pid_col, path_col, *label]].copy()  # only one label for evaluation
        # TODO: assert df[split_col].str.contains('test', case=False).all()

    if args.task == 'cls':
        predictions.loc[:, 'rounded_preds'] = (preds[0].squeeze().numpy() > 0.5).astype(int)

    predictions.loc[:, 'preds'] = preds[0].squeeze().numpy()

    predictions.to_csv(results_file, index=False)

    logger.info(f'Predictions are saved at:\n{results_file}')
    return preds


def wrap_up(out_dir, e=None):
    with open(f'{out_dir}/done_{script_name}', 'w') as f:
        if e is None:
            # done file should be empty when successful
            logger.info('Done successfully!')
            logger.info('_' * 100 + '\n')
        else:
            f.write(f'{e}\n')
            raise e


def setup_slivit_logger(args):
    logger = None
    if not args.slivit_exp:
        return logger
    script_name = get_script_name()
    # basic log setting
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S',
                        handlers=[
                            logging.FileHandler(f'{args.slivit_out_dir}/{script_name}.log', mode='w'),
                            logging.StreamHandler()  # Log messages to the console
                        ])

    logger = logging.getLogger()
    return logger

