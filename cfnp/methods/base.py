import pytorch_lightning as pl
import cfnp.modules.conv as conv
from cfnp.modules.gumble import GumbleCompressionModule
from typing import Any, Callable, Dict, List, Sequence, Tuple
import torch
from argparse import ArgumentParser
import numpy as np
from collections import Counter
from cfnp.utils.helper import bit_to_str

class BaseModel(pl.LightningModule):
    def __init__(
        self,
        X_fit: np.array,
        n_compressed: float,
        module: str,
        max_epochs: int,
        batch_size: int,
        optimizer: str,
        lr: float,
        weight_decay: float,
        momentum: float,
        scheduler: str = 'cosine',
        **kwargs
    ):
        super().__init__()

        self.batch_size = batch_size
        self.max_epochs = max_epochs
        self.optimizer = optimizer
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.scheduler = scheduler

        # X_fit = X_fit_to_tensor(X_fit)
        # self.register_buffer('X_fit', X_fit)
        self.register_buffer('X_fit', torch.Tensor(X_fit))
        self.n_fit = self.X_fit.size(0)
        self.n_features = self.X_fit.size(1)
        self.n_compressed = n_compressed
        self.module_name = module

        self.extra_kwargs = kwargs

        # 初始化压缩模块和相关参数
        if 'resnet' in module:
            self.module = getattr(conv, module)(self.n_features, self.n_compressed)
            self.X_fit = self.X_fit.view(1, self.X_fit.size(1), self.X_fit.size(0), 1)
        else:
            self.module = GumbleCompressionModule()
    
    @staticmethod
    def add_specific_args(parent_parser: ArgumentParser):
        parser = parent_parser.add_argument_group('base')

        # general
        parser.add_argument("--batch_size", type=int, default=64)
        parser.add_argument("--num_workers", type=int, default=8)

        # compression
        parser.add_argument("--cmp_ratio", type=float, default=None)
        parser.add_argument('--limited_memory', type=str, default=None)
        parser.add_argument('--limited_ratio', type=float, default=0.1)

        # hyper-opt
        parser.add_argument("--max_evals", type=int, default=100)

        # optimizer
        parser.add_argument("--optimizer", type=str, default='adam')
        parser.add_argument("--lr", type=float, default=0.01)
        parser.add_argument("--momentum", type=float, default=0.9)
        parser.add_argument("--weight-decay", type=float, default=1e-4)


        return parent_parser
    @property
    def learnable_params(self) -> List[Dict[str, Any]]:
        return [
            {"name": "module", "params": self.module.parameters()},
        ]

    def configure_optimizers(self) -> Tuple[List, List]:
        if self.optimizer == 'sgd':
            optimizer = torch.optim.SGD(
                self.learnable_params,
                lr=self.lr,
                momentum=self.momentum,
                weight_decay=self.weight_decay,
                nesterov=True
                )
        elif self.optimizer == 'adam':
            optimizer = torch.optim.Adam(
                self.learnable_params, 
                lr=self.lr, 
                betas=(0.9, 0.999), 
                eps=1e-08
                )
        else:
            assert False, f"{self.optimizer} not in (sgd, adam)"

        scheduler_dict = {
                "scheduler": torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, self.max_epochs),
                "interval": "epoch"
                }
        return {'optimizer':optimizer, 'lr_scheduler':scheduler_dict}
    
    def forward(self):

        return self.module(self.X_fit)

    def on_train_end(self):
        memory_need = max(self.n_compressed*(self.n_features+1)*64, self.n_compressed*self.n_compressed*64)
        memory_need = bit_to_str(memory_need)
        # if memory_need < 1024:
        #     memory_need = str(round(memory_need,2)) + 'B'
        # elif memory_need < 1024*1024:
        #     memory_need = str(round(memory_need/1024, 2)) + 'KB'
        # elif memory_need < 1024*1024*1024:
        #     memory_need = str(round(memory_need/1024/1024, 2)) + 'MB'
        # else:
        #     memory_need = str(round(memory_need/1024/1024/1024, 2)) + 'GB'
        self.logger.log_metrics({
            'n_features': self.n_features,
            'actual_ratio': 1-(self.n_compressed-self.n_fit),
            'before_n_ins': self.n_fit,
            'after_n_ins': self.n_compressed,
            'max_memory_used': memory_need
        })
    
    @staticmethod
    def build_original_model():
        pass
    
    @staticmethod
    def predict_by_params():
        pass

class ClassSeparationBaseModel(BaseModel):
    '''
    类分离，正负类各自输入到各自的module中
    init中对输入进行类分离
    forward中对输出进行合并
    coef_fit 可能无法提供良好的初始化
    '''
    def __init__(self, coef_fit, **kwargs):
        super(ClassSeparationBaseModel, ClassSeparationBaseModel).__init__(**kwargs)
        
        # 分离根据coef_fit的符号分离X_fit
        coef_sign = np.sign(coef_fit)
        counter = sorted(Counter(coef_sign).items())
        n_positive = counter[0][1]
        n_negative = counter[1][1]
        self.n_positive_compressed = int(self.n_compressed * n_positive/(n_positive + n_negative))
        self.n_negative_compressed = self.n_compressed - self.n_positive_compressed
        self.X_fit_positive = self.X_fit.view(self.X_fit.size(2), self.X_fit.size(1))[coef_sign>0]
        self.X_fit_negative = self.X_fit.view(self.X_fit.size(2), self.X_fit.size(1))[coef_sign<0]

        # 初始化module和相关参数
        del self.module
        if 'resnet' in self.module_name:
            self.module_processing_positve = getattr(conv, self.module_name)(self.n_features, self.n_positive_compressed)
            self.module_processing_negative = getattr(conv, self.module_name)(self.n_features, self.n_negative_compressed)
            self.X_fit_positive = self.X_fit_positive.view(1, self.X_fit_positive.size(1), self.X_fit_positive.size(0), 1)
            self.X_fit_negative = self.X_fit_negative.view(1, self.X_fit_negative.size(1), self.X_fit_negative.size(0), 1)
        else:
            self.module_processing_positve = GumbleCompressionModule()
            self.module_processing_negative = GumbleCompressionModule()

    def forward(self):
        X_compression_postive = self.module_processing_positve(self.X_fit_positive)
        X_compression_negative = self.module_processing_negative(self.X_fit_negative)
        X_compression = torch.cat((X_compression_postive, X_compression_negative), dim=0) # 按行拼接
        return X_compression

    @property
    def learnable_params(self) -> List[Dict[str, Any]]:
        return [
            {"name": "module_processing_positive", "params": self.module_processing_positve.parameters()},
            {"name": "module_processing_negative", "params": self.module_processing_negative.parameters()},
        ]

class AddInstanceBaseModel(BaseModel):
    '''
    添加一些原有实例到压缩实例集合中
    原有实例对应的coef_fit应该是会变动的
    '''
    def __init__(self, add_ratio, **kwargs):
        super(ClassSeparationBaseModel, ClassSeparationBaseModel).__init__(**kwargs)
        self.n_add = int(self.n_compressed * add_ratio)
        self.n_compressed = self.n_compressed - self.n_add
        
        randints = np.random.randint(0, self.n_fit, (self.n_add,))
        self.X_add = self.X_fit.view(self.X_fit.size(2), self.X_fit.size(1))[randints]

        # 重新初始化module
        if 'resnet' in self.module_name:
            self.module = getattr(conv, self.module_name)(self.n_features, self.n_compressed)
            self.X_fit = self.X_fit.view(1, self.X_fit.size(1), self.X_fit.size(0), 1)
        else:
            self.module = GumbleCompressionModule()
    
    @staticmethod
    def add_specific_args(parent_parser: ArgumentParser):
        parser = parent_parser.add_argument_group('add_base')
        parser.add_argument('--add_ratio', type=float, required=True)
        return parent_parser

    @property
    def learnable_params(self) -> List[Dict[str, Any]]:
        return [
            {"name": "module", "params": self.module.parameters()},
        ]

    def forward(self):
        X_compression = torch.cat((self.module(self.X_fit), self.X_add),dim=0)
        return X_compression
