import os
import torch
import numpy as np
import pickle as pkl
from torch.utils.data import Dataset
from PIL import Image
from torchvision import transforms # type: ignore
import torch.nn.functional as F
import matplotlib.pyplot as plt
import json
from monai import transforms as monai_transforms
from .PatientDataset import PatientDatasetCenter2D, PatientDataset3D # , create_3d_transforms

home_directory: str = os.getenv('HOME')

def check_patient_in_multi_task_idx(disease_list, multi_task_idx):
    new_disease_list =[disease_list[0]]
    for idx in multi_task_idx:
        new_disease_list.append(disease_list[idx])

    if sum(new_disease_list) > 0:
        include = True
    else:
        include = False
    return include, new_disease_list

def get_file_list_given_patient_and_visit_hash(patient_id, visit_hash, mode='oct_img', prefix='', midfix='/macOCT/', num_frames=61):
    dir_name = prefix + patient_id + midfix + visit_hash
    oct_file_list = []
    if mode == 'oct_img':
        for i in range(num_frames):
            frame = dir_name + '/oct-%03d.png' % i
            oct_file_list.append(frame)
    elif mode == 'ir_img':
        oct_file_list = [dir_name + '/ir.png']
    return oct_file_list

def create_3d_transforms(input_size, num_frames=64, RandFlipd_prob=0.5, RandRotate90d_prob=0.5, normalize_dataset=False, **kwargs):
    train_compose = [
            monai_transforms.CropForegroundd(keys=["pixel_values"], source_key="pixel_values"),
            monai_transforms.Resized(
                keys=["pixel_values"], spatial_size=(num_frames, input_size, input_size), mode=("trilinear")
            ),
            monai_transforms.RandFlipd(keys=["pixel_values"], prob=RandFlipd_prob, spatial_axis=0),
            monai_transforms.RandFlipd(keys=["pixel_values"], prob=RandFlipd_prob, spatial_axis=2),
        ]
    val_compose = [
            monai_transforms.Resized(
                keys=["pixel_values"], spatial_size=(num_frames, input_size, input_size), mode=("trilinear")
            ),

        ]
    if normalize_dataset:
        train_compose.append(monai_transforms.NormalizeIntensityd(keys=["pixel_values"], subtrahend=0.25, divisor=0.25, nonzero=True))
        val_compose.append(monai_transforms.NormalizeIntensityd(keys=["pixel_values"], subtrahend=0.25, divisor=0.25, nonzero=True))
        print('Normalize the dataset in 3d transform!')

    # create the transform function
    train_transform = monai_transforms.Compose(
        train_compose
    )

    val_transform = monai_transforms.Compose(
        val_compose
    )

    return train_transform, val_transform


class PatientDatasetCenter2D_inhouse(PatientDatasetCenter2D):
    def __init__(self, root_dir, task_mode='binary_cls', disease='AMD', disease_name_list=None, metadata_fname=None, dataset_mode='frame', transform=None, convert_to_tensor=False, return_patient_id=False, out_frame_idx=False, name_split_char='-', iterate_mode='visit', downsample_width=True, mode='rgb', patient_id_list_dir='multi_cls_expr_10x/', downsample_normal=False, downsample_normal_factor=10, multi_task_idx=None, **kwargs):
        """
        Args:
            root_dir (string): Directory with all the images.
            task_mode (str): 'binary_cls', 'multi_cls', 'multi_label'
            disease (str): 'AMD', 'DME', 'POG', 'MH'
            disease_name_list (list): list of disease names
            metadata_fname (str): metadata file name
            dataset_mode (str): 'frame', 'volume'
            transform (callable, optional): Optional transform to be applied on a sample.
            convert_to_tensor (bool): If True, convert the image to tensor
            return_patient_id (bool): If True, return the patient_id
            out_frame_idx (bool): If True, return the frame index
            name_split_char (str): split character for the name
            iterate_mode (str): 'visit' or 'patient'
            downsample_width (bool): If True, downsample the width to 512 (1024) / 768 (1536)
            mode (str): 'rgb', 'gray'

        """
        super().__init__(root_dir, patient_idx_loc=0, dataset_mode=None, transform=transform, downsample_width=downsample_width, convert_to_tensor=convert_to_tensor, return_patient_id=return_patient_id, out_frame_idx=out_frame_idx, name_split_char=name_split_char, cls_unique=False, iterate_mode=iterate_mode, **kwargs)

        self.mode = mode
        self.task_mode = task_mode
        self.downsample_width = downsample_width
        self.dataset_mode = dataset_mode
        self.set_disease_availability()
        self.multi_task_idx = multi_task_idx
        print(self.root_dir)
        root_dir = '/'.join(self.root_dir.split('/')[:-2]) + '/'
        print(root_dir)
        self.set_filepath(root_dir=root_dir, patient_id_list_dir=patient_id_list_dir)
        if disease_name_list is None:
            disease_name_list = self.available_disease
        # Task mode can be 'binary_cls', 'multi_cls', 'multi_label'
        self.set_task_mode(task_mode=self.task_mode, disease=disease, disease_name_list=disease_name_list)

        if metadata_fname is None:
            self.load_metadata()
        else:
            self.load_metadata(metadata_fname)

        self.load_patient_id_list()
        self.patients, self.visits_dict, self.mapping_patient2visit, self.mapping_visit2patient = self._get_patients()
        self.normal_patient_idx, self.normal_visit_idx, self.abnormal_patient_idx, self.abnormal_visit_idx = self.get_all_normal_patient_idx()
        self.downsample_normal = downsample_normal
        self.downsample_normal_factor = downsample_normal_factor
        if self.downsample_normal:
            self.epoch_seed = 0
            self.rng = np.random.default_rng(seed = self.epoch_seed)
            self.adjusted_indices = self.adjust_normal_indices(shuffle=False)

    def set_disease_availability(self):
        self.available_disease = ['AMD', 'DME', 'POG', 'ODR', 'PM', 'CRO', 'RN', 'VD']

    def set_task_mode(self, task_mode='binary_cls', disease='AMD', disease_name_list=['AMD', 'DME', 'POG', 'MH']):
        '''
        Args:
        task_mode (str): 'binary_cls', 'multi_cls', 'multi_label'
        disease (str): 'AMD', 'DME', 'POG', 'MH'
        disease_name_list (list): list of disease names

        Description: Set the task mode and disease name for the dataset
        '''
        self.task_mode = task_mode
        if self.task_mode == 'binary_cls':
            # currently only supportes disease vs. non-disease
            assert disease in self.available_disease
            self.disease = disease
            self.class_to_idx = {'NC': 0, disease: 1}
            self.idx_to_class = {0: 'NC', 1: disease}
        elif self.task_mode == 'multi_cls':
            self.disease_name_list = disease_name_list
            # [assert disease_name in self.available_disease for disease_name in disease_name_list]
            self.class_to_idx = {disease_name: idx for idx, disease_name in enumerate(disease_name_list)}
        elif self.task_mode == 'multi_label':
            self.disease_name_list = disease_name_list
            # [assert disease_name in self.available_disease for disease_name in disease_name_list]
            self.class_to_idx = {disease_name: idx for idx, disease_name in enumerate(disease_name_list)}
        elif self.task_mode == 'multi_task' or self.task_mode == 'multi_task_default':
            self.disease_name_list = disease_name_list
            # [assert disease_name in self.available_disease for disease_name in disease_name_list]
            self.class_to_idx = {disease_name: idx for idx, disease_name in enumerate(disease_name_list)}


    def set_filepath(self, metadata_dir='Oph_cls_task/', patient_id_list_dir='multi_cls_expr_10x/',
        root_dir=home_directory+'/OCTCubeM/assets/'):
        self.metadata_dir = root_dir + metadata_dir
        self.patient_id_list_dir = root_dir + metadata_dir + patient_id_list_dir

    def load_metadata(self, patient_dict_w_metadata_fname='patient_dict_w_metadata_first_visit_from_ir.pkl'):
        self.patient_dict_w_metadata_fname = patient_dict_w_metadata_fname
        with open(self.metadata_dir + self.patient_dict_w_metadata_fname, 'rb') as f:
            self.patient_dict_w_metadata = pkl.load(f)
        print('patient_dict_w_metadata:', len(self.patient_dict_w_metadata))
        # for key, value in self.patient_dict_w_metadata.items():
        #     print(key, value)
        #     exit()

        # self.patient_dict_laterality_fname = 'patient_dict_laterality.pkl'
        # with open(self.metadata_dir + self.patient_dict_laterality_fname, 'rb') as f:
        #     self.patient_dict_laterality = pkl.load(f)

    def load_patient_id_list(self, use_all=True):
        '''
        Args:
        use_all (bool): If True, use all the patient ids from the metadata, only for multi_label
        '''

        if self.task_mode == 'binary_cls':
            patient_w_disease_fname = self.patient_id_list_dir + self.disease + '_w_disease.txt'
            patient_wo_disease_fname = self.patient_id_list_dir + self.disease + '_wo_disease.txt'
            with open(patient_w_disease_fname, 'r') as f:
                self.patient_w_disease = f.readlines()
                for i, line in enumerate(self.patient_w_disease):

                    self.patient_w_disease[i] = line.strip()

            with open(patient_wo_disease_fname, 'r') as f:
                self.patient_wo_disease = f.readlines()
                for i, line in enumerate(self.patient_wo_disease):
                    self.patient_wo_disease[i] = line.strip()
            print(len(self.patient_w_disease), len(self.patient_wo_disease))

        elif self.task_mode == 'multi_cls':
            raise NotImplementedError
        elif self.task_mode == 'multi_label' or self.task_mode == 'multi_task' or self.task_mode == 'multi_task_default':
            assert self.patient_dict_w_metadata is not None
            if use_all:

                patient_id_w_multilabel = self.patient_id_list_dir + 'multilabel_cls_dict.json'
                with open(patient_id_w_multilabel, 'r') as f:
                    self.patient_id_w_multilabel = json.load(f)
                    self.disease_list = self.patient_id_w_multilabel['disease_list']
                    self.idx_to_disease = {idx: disease for idx, disease in enumerate(self.disease_list)}
                    self.patient_id_list = self.patient_id_w_multilabel['patient_dict']
                    # patient_id_list is a dict, sort it by the key
                    self.patient_id_list = dict(sorted(self.patient_id_list.items()))

            else:
                raise NotImplementedError

    def _get_patients(self):
        patients = {}
        if self.task_mode == 'binary_cls':
            patient_id_list = self.patient_w_disease + self.patient_wo_disease
            label = np.array([1] * len(self.patient_w_disease) + [0] * len(self.patient_wo_disease))

            visits_dict = {}
            mapping_patient2visit = {}
            visit_idx = 0
            for patient_id, label in zip(patient_id_list, label):
                patients[patient_id] = {'class_idx': [], 'class': [], 'frames': []}
                visits = self.patient_dict_w_metadata[patient_id]
                for visit in visits:
                    patients[patient_id]['class_idx'].append(label)
                    patients[patient_id]['class'].append(self.idx_to_class[label])
                    fname_list = get_file_list_given_patient_and_visit_hash(patient_id, visit)
                    patients[patient_id]['frames'].append(fname_list)
                    visits_dict[visit_idx] = {'class_idx': label, 'class': self.idx_to_class[label], 'frames': fname_list, 'visit_hash': visit}
                    if patient_id not in mapping_patient2visit:
                        mapping_patient2visit[patient_id] = [visit_idx]
                    else:
                        mapping_patient2visit[patient_id].append(visit_idx)
                    visit_idx += 1
            mapping_visit2patient = {visit_idx: patient_id for patient_id, visit_idx_list in mapping_patient2visit.items() for visit_idx in visit_idx_list}
            return patients, visits_dict, mapping_patient2visit, mapping_visit2patient

        elif self.task_mode == 'multi_label' or self.task_mode == 'multi_task' or self.task_mode == 'multi_task_default':
            assert self.patient_dict_w_metadata is not None
            assert self.patient_id_list is not None
            visits_dict = {}
            mapping_patient2visit = {}
            visit_idx = 0

            for patient_id, disease_list in self.patient_id_list.items():
                class_list = [self.idx_to_disease[i] for i in range(len(disease_list))]
                if self.multi_task_idx is not None:
                    include, new_disease_list = check_patient_in_multi_task_idx(disease_list, self.multi_task_idx)
                    new_class_list = [self.idx_to_disease[0]] + [self.idx_to_disease[i] for i in self.multi_task_idx]
                    if not include:
                        continue
                    else:
                        disease_list = new_disease_list
                        class_list = new_class_list
                patients[patient_id] = {'class_idx': [], 'class': [], 'frames': []}
                visits = self.patient_dict_w_metadata[patient_id]
                for visit in visits:

                    patients[patient_id]['class_idx'].append(np.array(disease_list))
                    patients[patient_id]['class'].append(class_list)
                    fname_list = get_file_list_given_patient_and_visit_hash(patient_id, visit)
                    patients[patient_id]['frames'].append(fname_list)
                    visits_dict[visit_idx] = {'class_idx': np.array(disease_list), 'class': [self.idx_to_disease[i] for i in range(len(disease_list))], 'frames': fname_list, 'visit_hash': visit}
                    if patient_id not in mapping_patient2visit:
                        mapping_patient2visit[patient_id] = [visit_idx]
                    else:
                        mapping_patient2visit[patient_id].append(visit_idx)

                    visit_idx += 1

            mapping_visit2patient = {visit_idx: patient_id for patient_id, visit_idx_list in mapping_patient2visit.items() for visit_idx in visit_idx_list}
            return patients, visits_dict, mapping_patient2visit, mapping_visit2patient


        elif self.task_mode == 'multi_cls':
            raise NotImplementedError


    def get_all_normal_patient_idx(self):
        normal_patient_idx = []
        normal_visit_idx = []
        abnormal_patient_idx = []
        abnormal_visit_idx = []

        if self.task_mode == 'binary_cls':
            print(self.patient_w_disease)
            normal_patient_idx = self.patient_w_disease
            for patient_id in self.patient_w_disease:
                normal_visit_idx += self.mapping_patient2visit[patient_id]
            abnormal_patient_idx = self.patient_wo_disease
            for patient_id in self.patient_wo_disease:
                abnormal_visit_idx += self.mapping_patient2visit[patient_id]

            return normal_patient_idx, normal_visit_idx, abnormal_patient_idx, abnormal_visit_idx
        elif self.task_mode == 'multi_label' or self.task_mode == 'multi_task' or self.task_mode == 'multi_task_default':
            for patient_id, label in self.patient_id_list.items():
                if self.multi_task_idx is not None:
                    include, new_disease_list = check_patient_in_multi_task_idx(label, self.multi_task_idx)
                    if not include:
                        continue
                if label == [1] + [0] * (len(label) - 1):
                    normal_patient_idx.append(patient_id)
                    normal_visit_idx += self.mapping_patient2visit[patient_id]
                else:
                    abnormal_patient_idx.append(patient_id)
                    abnormal_visit_idx += self.mapping_patient2visit[patient_id]

        return normal_patient_idx, normal_visit_idx, abnormal_patient_idx, abnormal_visit_idx

    def adjust_normal_indices(self, shuffle=False):
        adjusted_indices = []
        # Calculate the number of samples to select from the majority class
        num_samples = len(self.normal_visit_idx) // self.downsample_normal_factor

        # Randomly select indices from the majority class using the local generator
        adjusted_indices.append(self.rng.choice(self.normal_visit_idx, size=num_samples, replace=False))

        adjusted_indices = np.concatenate([adjusted_indices[0], self.abnormal_visit_idx])

        if shuffle:
            self.rng.shuffle(adjusted_indices)

        return adjusted_indices

    def on_epoch_end(self):
        # Increment epoch to change the seed for the next epoch
        self.epoch_seed += 1
        # Create a new random generator for the next epoch
        self.rng = np.random.default_rng(seed=self.epoch_seed)
        # Adjust the indices at the end of each epoch
        self.adjusted_indices = self.adjust_normal_indices()

    def get_visit_idx(self, patient_id_list):
        visit_idx_list = []
        for patient_id in patient_id_list:
            visit_idx_list += self.mapping_patient2visit[patient_id]
        return visit_idx_list

    def __len__(self):
        if self.downsample_normal:
            return len(self.adjusted_indices)
        else:
            return len(self.visits_dict)

    def __getitem__(self, idx):
        if self.iterate_mode == 'patient':

            raise NotImplementedError
        elif self.iterate_mode == 'visit':
            data_dict = self.visits_dict[idx]
            patient_id = self.mapping_visit2patient[idx]

        if self.dataset_mode == 'frame':
            num_frames = len(data_dict['frames'])
            # Determine the middle index
            middle_index = (num_frames // 2) - 1 if num_frames % 2 == 0 else num_frames // 2
            frame_path = data_dict['frames'][middle_index]

            # Load frame as 3 channel image
            frame = Image.open(self.root_dir + frame_path, mode='r')
            if self.mode == 'gray':
                frame = frame.convert("L")
            elif self.mode == 'rgb':
                frame = frame.convert("RGB")
            if self.downsample_width:
                if frame.size[0] == 1024:
                    frame = frame.resize((512, frame.size[1]))
                if frame.size[1] == 1024 or frame.size[1] == 1536:
                    frame = frame.resize((frame.size[0], frame.size[1] // 2))
            if self.transform:
                frame = self.transform(frame)
            # Convert frame to tensor (if not already done by transform)
            if self.convert_to_tensor and not isinstance(frame, torch.Tensor):
                frame = torch.tensor(np.array(frame), dtype=torch.float32)
                frame = frame.permute(2, 0, 1)
                print(frame.shape)

            if not self.out_frame_idx and not self.return_patient_id:
                return frame, data_dict['class_idx']
            elif not self.out_frame_idx and self.return_patient_id:
                return frame, data_dict['class_idx'], patient_id
            elif self.out_frame_idx and not self.return_patient_id:
                return frame, data_dict['class_idx'], (middle_index, num_frames)
            else:
                return frame, data_dict['class_idx'], patient_id, (middle_index, num_frames)
        elif self.dataset_mode == 'frame_inference_all':

            num_frames = len(data_dict['frames'])
            frames = [Image.open(self.root_dir + frame_path, mode='r') for frame_path in data_dict['frames']]
            if self.mode == 'gray':
                frames = [frame.convert("L") for frame in frames]
            elif self.mode == 'rgb':
                frames = [frame.convert("RGB") for frame in frames]
            if self.downsample_width:
                if frames[0].size[0] == 1024:
                    frames = [frame.resize((512, frame.size[1])) for frame in frames]
                if frames[0].size[1] == 1024 or frames[0].size[1] == 1536:
                    frames = [frame.resize((frame.size[0], frame.size[1] // 2)) for frame in frames]
            if self.transform:

                frames = [self.transform(frame) for frame in frames]
            # Convert frame to tensor (if not already done by transform)
            if self.convert_to_tensor and not isinstance(frames[0], torch.Tensor):
                frames = [torch.tensor(np.array(frame), dtype=torch.float32) for frame in frames]
                frames = [frame.permute(2, 0, 1) for frame in frames]
            frames_tensor = torch.stack(frames)
            if self.return_patient_id:
                return frames_tensor, (data_dict['class_idx'], patient_id, data_dict['visit_hash'])
            else:
                return frames_tensor, data_dict['class_idx']

        else:
            raise NotImplementedError



class PatientDataset3D_inhouse(PatientDatasetCenter2D_inhouse):
    def __init__(self, root_dir, task_mode='binary_cls', disease='AMD', disease_name_list=None, metadata_fname=None, dataset_mode='frame', transform=None, convert_to_tensor=False, return_patient_id=False, name_split_char='-', iterate_mode='visit', downsample_width=True, mode='rgb', patient_id_list_dir='multi_cls_expr_10x/', pad_to_num_frames=False, padding_num_frames=None, transform_type='frame_2D', same_3_frames=False, high_res_transform=None, return_both_res_image=False, high_res_num_frames=None, multi_task_idx=None, **kwargs):
        """
        Args:
            root_dir (string): Directory with all the images.
            task_mode (str): 'binary_cls', 'multi_cls', 'multi_label'
            disease (str): 'AMD', 'DME', 'POG', 'MH'
            disease_name_list (list): list of disease names
            metadata_fname (str): metadata file name
            dataset_mode (str): 'frame', 'volume'
            transform (callable, optional): Optional transform to be applied on a sample.
            convert_to_tensor (bool): If True, convert the image to tensor
            return_patient_id (bool): If True, return the patient_id
            out_frame_idx (bool): If True, return the frame index
            name_split_char (str): split character for the name
            iterate_mode (str): 'visit' or 'patient'
            downsample_width (bool): If True, downsample the width to 512 (1024) / 768 (1536)
            mode (str): 'rgb', 'gray'

        """
        super().__init__(root_dir, task_mode=task_mode, disease=disease, disease_name_list=disease_name_list, metadata_fname=metadata_fname, dataset_mode=dataset_mode, transform=transform, convert_to_tensor=convert_to_tensor, return_patient_id=return_patient_id, out_frame_idx=False, name_split_char=name_split_char, iterate_mode=iterate_mode, downsample_width=downsample_width, mode=mode, patient_id_list_dir=patient_id_list_dir, multi_task_idx=multi_task_idx, **kwargs)
        self.pad_to_num_frames = pad_to_num_frames
        self.padding_num_frames = padding_num_frames
        self.transform_type = transform_type
        self.same_3_frames = same_3_frames
        self.high_res_transform = high_res_transform
        self.return_both_res_image = return_both_res_image
        self.high_res_num_frames = high_res_num_frames


    def __getitem__(self, idx):
        if self.iterate_mode == 'patient':
            raise NotImplementedError
        elif self.iterate_mode == 'visit':
            data_dict = self.visits_dict[idx]
            patient_id = self.mapping_visit2patient[idx]

        if self.dataset_mode == 'frame' or self.dataset_mode == 'frame_inference_all':
            frames = [Image.open(self.root_dir + frame_path, mode='r') for frame_path in data_dict['frames']]
            if self.mode == 'rgb':
                frames = [frame.convert("RGB") for frame in frames]
            else:
                pass

            if self.downsample_width:
                for i, frame in enumerate(frames):
                    if frame.size[0] == 1024:
                        frames[i] = frame.resize((512, frame.size[1]))
                    if frame.size[1] == 1024 or frame.size[1] == 1536:
                        frames[i] = frame.resize((frame.size[0], frame.size[1] // 2))

            if self.transform and self.transform_type == 'frame_2D':
                frames = [self.transform(frame) for frame in frames]
                if self.return_both_res_image and self.high_res_transform:
                    frames_high_res = [self.high_res_transform(frame) for frame in frames]
            elif self.transform and self.transform_type == 'monai_3D':
                frames = [transforms.ToTensor()(frame) for frame in frames]
                if self.return_both_res_image and self.high_res_transform:
                    frames_high_res = frames

            # Convert frame to tensor (if not already done by transform)
            if self.convert_to_tensor and not isinstance(frames[0], torch.Tensor):
                frames = [torch.tensor(np.array(frame), dtype=torch.float32) for frame in frames]
                print(frames[0].shape)
                frames = [frame.permute(2, 0, 1) for frame in frames]

            frames_tensor = torch.stack(frames) # (num_frames, C, H, W)
            if self.return_both_res_image and self.high_res_transform:
                frames_tensor_high_res = torch.stack(frames_high_res)

            if self.pad_to_num_frames:
                assert self.padding_num_frames is not None
                num_frames = frames_tensor.shape[0]
                if num_frames < self.padding_num_frames:
                    left_padding = (self.padding_num_frames - num_frames) // 2
                    right_padding = self.padding_num_frames - num_frames - left_padding
                    left_padding = torch.zeros(left_padding, frames_tensor.shape[-3], frames_tensor.shape[-2], frames_tensor.shape[-1])
                    right_padding = torch.zeros(right_padding, frames_tensor.shape[-3], frames_tensor.shape[-2], frames_tensor.shape[-1])
                    frames_tensor = torch.cat([left_padding, frames_tensor, right_padding], dim=0)
                elif num_frames > self.padding_num_frames:
                    # get the frames from the middle
                    if self.same_3_frames:
                        assert self.padding_num_frames == 3, 'Only support 3 frames to mock 1 frame'
                        start_idx = (num_frames - 1) // 2
                        end_idx = start_idx + 1
                        frames_tensor = frames_tensor[start_idx:end_idx].repeat(3, 1, 1, 1)
                    else:
                        # perform center cropping
                        left_idx = (num_frames - self.padding_num_frames) // 2
                        right_idx = num_frames - self.padding_num_frames - left_idx
                        frames_tensor = frames_tensor[left_idx:-right_idx, :, :, :]
                else:
                    pass
                if self.return_both_res_image and self.high_res_transform:
                    if self.high_res_num_frames is None:
                        self.high_res_num_frames = self.padding_num_frames
                    if num_frames < self.high_res_num_frames:
                        high_res_left_padding = (self.high_res_num_frames - num_frames) // 2
                        high_res_right_padding = self.high_res_num_frames - num_frames - high_res_left_padding
                        left_paddings_high_res = torch.zeros(high_res_left_padding, frames_tensor_high_res.shape[-3], frames_tensor_high_res.shape[-2], frames_tensor_high_res.shape[-1])
                        right_paddings_high_res = torch.zeros(high_res_right_padding, frames_tensor_high_res.shape[-3], frames_tensor_high_res.shape[-2], frames_tensor_high_res.shape[-1])
                        frames_tensor_high_res = torch.cat([left_paddings_high_res, frames_tensor_high_res, right_paddings_high_res], dim=0)
                    elif num_frames > self.high_res_num_frames:
                        high_res_left_idx = (num_frames - self.high_res_num_frames) // 2
                        high_res_right_idx = num_frames - self.high_res_num_frames - high_res_left_idx
                        frames_tensor_high_res = frames_tensor_high_res[high_res_left_idx:-high_res_right_idx, :, :, :]

            if self.mode == 'gray':
                frames_tensor = frames_tensor.squeeze(1)
                if self.return_both_res_image and self.high_res_transform:
                    frames_tensor_high_res = frames_tensor_high_res.squeeze(1)

            if self.transform and self.transform_type == 'monai_3D':
                frames_tensor = frames_tensor.unsqueeze(0)

                frames_tensor = self.transform({"pixel_values": frames_tensor})["pixel_values"]


                if self.return_both_res_image and self.high_res_transform:
                    frames_tensor_high_res = frames_tensor_high_res.unsqueeze(0)
                    frames_tensor_high_res = self.high_res_transform({"pixel_values": frames_tensor_high_res})["pixel_values"]


            if self.return_patient_id:
                return (frames_tensor, frames_tensor_high_res) if self.return_both_res_image and self.high_res_transform else frames_tensor, (data_dict['class_idx'], patient_id, data_dict['visit_hash'])
            else:
                return (frames_tensor, frames_tensor_high_res) if self.return_both_res_image and self.high_res_transform else frames_tensor, data_dict['class_idx']

        else:
            raise NotImplementedError

