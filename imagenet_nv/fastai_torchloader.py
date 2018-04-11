import argparse
import os
import shutil
import time

from fastai.transforms import *
from fastai.dataset import *
from fastai.fp16 import *
from fastai.conv_learner import *
from pathlib import *

import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import models
from distributed import DistributedDataParallel as DDP

model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))
# print(model_names)

# Example usage: python run_fastai.py /home/paperspace/ILSVRC/Data/CLS-LOC/ -a resnext_50_32x4d --epochs 1 -j 4 -b 64 --fp16

parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--save-dir', type=str, default=Path.home()/'imagenet_training',
                    help='Directory to save logs and models.')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                    ' | '.join(model_names) +
                    ' (default: resnet18)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--cycle-len', default=1, type=float, metavar='N',
                    help='Length of cycle to run')
# parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
#                     help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=256, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M', help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
# parser.add_argument('--print-freq', '-p', default=10, type=int,
#                     metavar='N', help='print frequency (default: 10)')
# parser.add_argument('--resume', default='', type=str, metavar='PATH',
#                     help='path to latest checkpoint (default: none)')
# parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
#                     help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true', help='use pre-trained model')
parser.add_argument('--fp16', action='store_true', help='Run model fp16 mode.')
parser.add_argument('--use-tta', action='store_true', help='Validate model with TTA at the end of traiing.')
parser.add_argument('--train-128', action='store_true', help='Train model on 128. TODO: allow custom epochs and LR')
parser.add_argument('--sz',       default=224, type=int, help='Size of transformed image.')
# parser.add_argument('--decay-int', default=30, type=int, help='Decay LR by 10 every decay-int epochs')
parser.add_argument('--use-clr', type=str, 
                    help='div,pct,max_mom,min_mom. Pass in a string delimited by commas. Ex: "20,2,0.95,0.85"')
parser.add_argument('--loss-scale', type=float, default=1,
                    help='Loss scaling, positive power of 2 values can improve fp16 convergence.')
parser.add_argument('--prof', dest='prof', action='store_true', help='Only run a few iters for profiling.')

parser.add_argument('--dist-url', default='file://sync.file', type=str,
                    help='url used to set up distributed training')
parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')

parser.add_argument('--world-size', default=1, type=int,
                    help='Number of GPUs to use. Can either be manually set ' +
                    'or automatically set by using \'python -m multiproc\'.')
parser.add_argument('--rank', default=0, type=int,
                    help='Used for multi-process training. Can either be manually set ' +
                    'or automatically set by using \'python -m multiproc\'.')

def torch_loader(data_path, size):
    # Data loading code
    traindir = os.path.join(data_path, 'train')
    valdir = os.path.join(data_path, 'val')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])

    train_dataset = datasets.ImageFolder(
        traindir,
        transforms.Compose([
            transforms.RandomResizedCrop(size),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            normalize,
        ]))

    train_sampler = (torch.utils.data.distributed.DistributedSampler(train_dataset)
                     if args.distributed else None)

    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
        num_workers=args.workers, pin_memory=True, sampler=train_sampler)

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Resize(int(size*1.14)),
            transforms.CenterCrop(size),
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)
    
    train_loader = DataPrefetcher(train_loader)
    val_loader = DataPrefetcher(val_loader)
    if args.prof:
        train_loader.stop_after = 800
        val_loader.stop_after = 0

    data = ModelData(data_path, train_loader, val_loader)
    return data, train_sampler


# Seems to speed up training by ~2%
class DataPrefetcher():
    def __init__(self, loader, stop_after=None):
        self.loader = loader
        self.dataset = loader.dataset
        self.stream = torch.cuda.Stream()
        self.stop_after = stop_after
        self.next_input = None
        self.next_target = None

    def __len__(self):
        return len(self.loader)
    
    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loaditer)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        with torch.cuda.stream(self.stream):
            self.next_input = self.next_input.cuda(async=True)
            self.next_target = self.next_target.cuda(async=True)

    def __iter__(self):
        count = 0
        self.loaditer = iter(self.loader)
        self.preload()
        while self.next_input is not None:
            torch.cuda.current_stream().wait_stream(self.stream)
            input = self.next_input
            target = self.next_target
            self.preload()
            count += 1
            yield input, target
            if type(self.stop_after) is int and (count > self.stop_after):
                break
            
# Taken from main.py topk accuracy
def top5(output, target):
    """Computes the precision@k for the specified values of k"""
    batch_size = target.size(0)
    _, pred = output.topk(5, 1, True, True)
    pred = pred.t()
    return pred.eq(target.view(1, -1).expand_as(pred)).sum()/batch_size


class ImagenetLoggingCallback(Callback):
    def __init__(self, save_path, print_every=50):
        super().__init__()
        self.save_path=save_path
        self.print_every=print_every
    def on_train_begin(self):
        self.batch = 0
        self.epoch = 0
        self.f = open(self.save_path, "a", 1)
        self.log("\ton_train_begin")
    def on_epoch_end(self, metrics):
        log_str = f'\tEpoch:{self.epoch}\ttrn_loss:{self.last_loss}'
        for (k,v) in zip(['val_loss', 'acc', 'top5', ''], metrics): log_str += f'\t{k}:{v}'
        self.log(log_str)
        self.epoch += 1
    def on_batch_end(self, metrics):
        self.last_loss = metrics
        self.batch += 1
        if self.batch % self.print_every == 0:
            self.log(f'Epoch: {self.epoch} Batch: {self.batch} Metrics: {metrics}')
    def on_train_end(self):
        self.log("\ton_train_end")
        self.f.close()
    def log(self, string):
        self.f.write(time.strftime("%Y-%m-%dT%H:%M:%S")+"\t"+string+"\n")

# Logging + saving models
def save_args(name, save_dir):
    if (args.rank != 0) or not args.save_dir: return {}

    log_dir = f'{save_dir}/training_logs'
    os.makedirs(log_dir, exist_ok=True)
    return {
        'best_save_name': f'{name}_best_model',
        'cycle_save_name': f'{name}',
        'callbacks': [
            ImagenetLoggingCallback(f'{log_dir}/{name}_log.txt')
        ]
    }

def save_sched(sched, save_dir):
    if (args.rank != 0) or not args.save_dir: return {}
    log_dir = f'{save_dir}/training_logs'
    sched.save_path = log_dir
    sched.plot_loss()
    sched.plot_lr()

def update_model_dir(learner, base_dir):
    learner.tmp_path = f'{base_dir}/tmp'
    os.makedirs(learner.tmp_path, exist_ok=True)
    learner.models_path = f'{base_dir}/models'
    os.makedirs(learner.models_path, exist_ok=True)
    
# This is important for speed
cudnn.benchmark = True
global args
args = parser.parse_args()
print('Running script with args:', args)

def main():
    args.distributed = args.world_size > 1
    args.gpu = 0
    if args.distributed:
        args.gpu = args.rank % torch.cuda.device_count()

    if args.distributed:
        torch.cuda.set_device(args.gpu)
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size)

    if args.fp16:
        assert torch.backends.cudnn.enabled, "fp16 mode requires cudnn backend to be enabled."

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    model = model.cuda()
    if args.distributed:
        model = DDP(model)
        
    if args.train_128:
        data, train_sampler = torch_loader(f'{args.data}-160', 128)
    else:
        data, train_sampler = torch_loader(args.data, args.sz)

    learner = Learner.from_model_data(model, data)
    learner.crit = F.cross_entropy
    learner.metrics = [accuracy, top5]
    if args.fp16: learner.half()
        
    if args.prof:
        args.epochs = 1
        args.cycle_len=1
    if args.use_clr:
        args.use_clr = tuple(map(float, args.use_clr.split(',')))
    
    # 128x128
    if args.train_128:
        save_dir = args.save_dir+'/128'
        update_model_dir(learner, save_dir)
        sargs = save_args('first_run_128', save_dir)
        learner.fit(args.lr,args.epochs, cycle_len=args.cycle_len,
                    sampler=train_sampler,
                    wds=args.weight_decay,
                    use_clr_beta=args.use_clr,
                    loss_scale=args.loss_scale,
                    **sargs
                )
        save_sched(learner.sched, save_dir)
        data, train_sampler = torch_loader(args.data, args.sz)
        learner.set_data(data)


    # Full size
    update_model_dir(learner, args.save_dir)
    sargs = save_args('first_run', args.save_dir)
    learner.fit(args.lr,args.epochs, cycle_len=args.cycle_len,
                sampler=train_sampler,
                wds=args.weight_decay,
                use_clr_beta=args.use_clr,
                loss_scale=args.loss_scale,
                **sargs
               )
    save_sched(learner.sched, args.save_dir)

    if args.use_tta:
        print(accuracy(*learner.TTA()))
        
    print('Finished!')
    
main()