from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, List, Tuple, Type, Dict, Union, Optional

import numpy as np

import torch
from torch.utils.data import DataLoader
from torch.optim import SGD

from .callbacks import Callback, Callbacks, ProgressBarCallback, \
    PredictionsSaverCallback, OneCycleLR, SimpleLossCallback, MetricsCallback, \
    TBLogger, LRFinder, CheckpointSaverCallback, SimpleSchedulerCallback, \
    EarlyStoppingCallback, SimpleOptimizerCallback
from .data import DataOwner
from .parallel import DataParallelCriterion, DataParallelModel
from .utils import DotDict, freeze_to, freeze, unfreeze


class Keker:
    """ The class serving the whole train-val-predict process.

    Args:
        model: The neural network for train/val/predict.
        dataowner: The namedtuple container of train/val/test dataloaders.
        tarket_key: The target/label key for batch-dict from dataloader.
            The dataloader returns batch as a dict on each iteration,
            that contains input data and target labels. This is key is for
            access to target labels in this dict.
        preds_key: The key for dict from self.step() functions.
            The self.step() function returns dict of predictions on each batch.
            This key is for access to predictions in this dict.
        criterion: The loss function or the dict {'name': loss function}
            in case of multiple loss setup. If multiple loss is using,
            loss_cb should be provided.
            (ex. : torch.nn.CrossEntropyLoss(),
            {"ce": torch.nn.CrossEntropyLoss(), "bce": torch.nn.BCE()})
        metrics: {"name": metric_function} dict, that contains callable metrics
            for calculating. A metric takes target and predictions
            tensors as parameters, and returns float.
            For examples see kekas.metrics module.
        opt: pytorch Optimizer class (ex. torch.optim.SGD, torch.optm.Adam, etc)
            This optimizer will be used as default during training.
            Default optimizer is torch.optim.SGD.
        opt_params: The kwargs dict for optimizer initialization.
            It should contain any optimizer param you want EXCEPT learning rate,
            learing rate is specified in self.kek* methods.
        device: The torch.device when you want to put your model
        step_fn: The function that will be called at every batch of your data.
            Take a `self` as a parameter. In this function you define
            what you do with your batch. You get access to batch through
            self._state.batch object. Batch is a dict returned from your
            dataloader, and its key and values are specified in reader_function
            for DataKek.
            Return a dict that should contain batch predictions with access
            by `preds_key`.
            For example see self.default_step method and example ipynbs.
        loss_cb: The Callback for loss calculation. Define how to calculate loss
            and store loss value in self._state.loss attr.
            Default loss callback is SimpleLossCallback.
            For examples see kekas.callbacks.
        opt_cb: The Callback for optimizer applying.
            Default optimizer callback is SimpleOptimizerCallback.
            For examples see kekas.callbacks.
        callbacks: custom Callbacks thet will be applied before the core ones.
            For examples see example ipynbs.
    """
    def __init__(self,
                 model: torch.nn.Module,
                 dataowner: DataOwner,
                 criterion: Union[torch.nn.Module, Dict[str, torch.nn.Module]],
                 target_key: str = "label",
                 preds_key: str = "logits",
                 metrics: Optional[Dict[str, Callable]] = None,
                 opt: Optional[Type[torch.optim.Optimizer]] = None,
                 opt_params: Optional[Dict] = None,
                 device: Optional[torch.device] = None,
                 step_fn: Optional[Callable] = None,
                 loss_cb: Optional[Callback] = None,
                 opt_cb: Optional[Callback] = None,
                 callbacks: Optional[Union[List, Callbacks]] = None) -> None:

        # The _state is an object that stores many variables and represents
        # the state of your train-val-repdict pipeline. _state passed to every
        # callback call.
        # You can use it as a container for your custom variables, but
        # DO NOT USE the following ones:
        #
        # loss, batch, model, dataowner, criterion, opt, parallel, checkpoint,
        # stop_iter, stop_epoch, stop_train, out, sched, mode, loader, pbar,
        # metrics, epoch_metrics
        #
        self._state = DotDict()

        self._state.model = model
        self._state.dataowner = dataowner

        self.target_key = target_key
        self.preds_key = preds_key

        self._state.criterion = criterion

        self._state.parallel = False
        if torch.cuda.device_count() > 1:
            self._state.model = DataParallelModel(self._state.model)
            self._state.criterion = DataParallelCriterion(self._state.criterion)
            self._state.parallel = True

        self.opt = opt or SGD
        self.opt_params = opt_params or {}
        self.device = device or torch.device("cuda" if
                                             torch.cuda.is_available()
                                             else "cpu")
        self._state.model.to(self.device)

        self.step = step_fn or self.default_step

        # the core callbacks for train-val-predict are determined here.
        # the order of callbacks is matters!
        loss_cb = loss_cb or SimpleLossCallback(target_key, preds_key)
        opt_cb = opt_cb or SimpleOptimizerCallback()
        metrics_cb = MetricsCallback(target_key, preds_key, metrics)

        callbacks = callbacks or []
        self.core_callbacks = callbacks + [loss_cb,
                                           metrics_cb,
                                           opt_cb,
                                           ProgressBarCallback()]
        callbacks = self.core_callbacks[:]

        self.callbacks = Callbacks(callbacks)

        self._state.checkpoint = ""

        # The number of batch in dataloader for iteration stop,
        # determined in self.kek* methods.
        self._state.stop_iter = None

        # flag for train stop after batch.
        self._state.stop_epoch = False

        # flag for stop the whole train, used for early stopping.
        self._state.stop_train = False

        # The scheduler attribute. Scheduler is determined in self.kek* methods.
        self._state.sched = None

    def kek(self,
            lr: float,
            epochs: int,
            skip_val: bool = False,
            opt: Optional[Type[torch.optim.Optimizer]] = None,
            opt_params: Optional[Dict] = None,
            sched: Optional[Callable] = None,
            sched_params: Optional[Dict] = None,
            sched_reduce_metric: Optional[str] = None,
            stop_iter: Optional[int] = None,
            logdir: Optional[Union[str, Path]] = None,
            cp_saver_params: Optional[Dict] = None,
            early_stop_params: Optional[Dict] = None) -> None:
        """Keks your model to the moon.

        Conducts a standard train-val procedure with several options for
        customization.

        Args:
            lr: learining rate
            epochs: number of epochs to train
            opt: torch optimizer. If specified, than specified optimizer will be
                used for this train-val procedure, else the default one.
            opt_params: The kwargs dict for custom optimizer initialization.
                It should contain any opt params you want EXCEPT learning rate,
                Can be defined even for default optimizer.
            sched: optional pytorch scheduler class. If specified, sched_params
                must be specified too.
                Ex: torch.optim.lr_scheduler.StepLR.
            sched_params: kwargs dict parameters for scheduler
            sched_reduce_metric: a unique parameter for ReduceLROnPlateau.
                Defines a metric to watch for learning rate reducing.
                If ReduceLROnPlateau used, but no sched_reduce_metric provided,
                then 'val_loss' metric is used. Else, it should be one of
                the metrics dict keys.
            stop_iter: number of batch when you want to end an epoch
            logdir: If provided, the TBLogger will be created and tensorboard
                logs will be written in this directory.
                For more info see kekas.callbacks.TBLogger and example ipynb's.
            cp_saver_params: kwargs dict parameters for CheckpointSaverCallback.
                If provided, then a CheckpointSaverCallback will be created.
                For more info see kekas.callbacks.CheckpointSaverCallback
                and example ipynb's.
            early_stop_params: kwargs dict parameters for EarlyStoppingCallback.
                If provided, then a EarlyStoppingCallback will be created.
                For more info see kekas.callbacks.EarlyStoppingCallback
                and example ipynb's.
        """

        if stop_iter:
            self.stop_iter = stop_iter

        # save callbacks
        callbacks = self.callbacks

        opt = opt or self.opt
        opt_params = opt_params or self.opt_params
        params = (p for p in self._state.model.parameters() if p.requires_grad)
        self._state.opt = opt(params=params, lr=lr, **opt_params)

        if sched:
            sched_params = sched_params or {}
            self._state.sched = sched(optimizer=self._state.opt, **sched_params)
            sched_cb = SimpleSchedulerCallback(sched=self._state.sched,
                                               metric=sched_reduce_metric)
            self.callbacks = Callbacks(self.callbacks.callbacks + [sched_cb])

        if logdir:
            self._state.do_log = True
            self._state.metrics = defaultdict(dict)
            tboard_cb = TBLogger(logdir)
            self.callbacks = Callbacks(self.callbacks.callbacks + [tboard_cb])

        cp_saver_params = cp_saver_params or {}
        if cp_saver_params:
            cp_saver_cb = CheckpointSaverCallback(**cp_saver_params)
            self.callbacks = Callbacks(self.callbacks.callbacks + [cp_saver_cb])

        early_stop_params = early_stop_params or {}
        if early_stop_params:
            early_stop_cb = EarlyStoppingCallback(**early_stop_params)
            self.callbacks = Callbacks(self.callbacks.callbacks + [early_stop_cb])

        # try-finally to properly close progress bar and restore callbacks
        try:
            self.callbacks.on_train_begin(self._state)

            for epoch in range(epochs):
                self.set_mode("train")
                self._run_epoch(epoch, epochs)

                if not skip_val:
                    self.set_mode("val")
                    self._run_epoch(epoch, epochs)

                if self._state.stop_train:
                    self._state.stop_train = False
                    print(f"Early stopped on {epoch + 1} epoch")
                    break

            self.callbacks.on_train_end(self._state)
        finally:
            self._state.pbar.close()
            self.callbacks = callbacks

    def kek_one_cycle(self,
                      max_lr: float,
                      cycle_len: int,
                      momentum_range: Tuple[float, float] = (0.95, 0.85),
                      div_factor: float = 25,
                      increase_fraction: float = 0.3,
                      opt: Optional[Type[torch.optim.Optimizer]] = None,
                      opt_params: Optional[Dict] = None,
                      logdir: Optional[Union[str, Path]] = None,
                      cp_saver_params: Optional[Dict] = None,
                      early_stop_params: Optional[Dict] = None) -> None:
        """Kek your model to the moon with One Cycle policy!

        Conducts a one-cycle train-val procedure with several options for
        customization.
        For info about One Cycle policy please see:
        https://arxiv.org/abs/1803.09820
        https://sgugger.github.io/the-1cycle-policy.html

        Args:
            max_lr: the maximum learning rate that will be achieved during
                training process
            cycle_len: the number of full passes through the training dataset.
                It is quite similar to epochs number
            momentum_range: the range of optimizers momentum changes
            div_factor: is equal to max_lr / min_lr during one-cycle training
            increase_fraction: the fraction of the whole iterations during which
                the learning rate will increase
            opt: torch optimizer. If specified, than specified optimizer will be
                used for this train-val procedure, else the default one.
            opt_params: The kwargs dict for custom optimizer initialization.
                It should contain any opt params you want EXCEPT learning rate,
                Can be defined even for default optimizer.
            logdir: If provided, the TBLogger will be created and tensorboard
                logs will be written in this directory.
                For more info see kekas.callbacks.TBLogger and example ipynb's.
            cp_saver_params: kwargs dict parameters for CheckpointSaverCallback.
                If provided, then a CheckpointSaverCallback will be created.
                For more info see kekas.callbacks.CheckpointSaverCallback
                and example ipynb's.
            early_stop_params: kwargs dict parameters for EarlyStoppingCallback.
                If provided, then a EarlyStoppingCallback will be created.
                For more info see kekas.callbacks.EarlyStoppingCallback
                and example ipynb's.
        """

        callbacks = self.callbacks

        # temporarily add OneCycle callback
        len_loader = len(self._state.dataowner.train_dl)
        one_cycle_cb = OneCycleLR(max_lr, cycle_len, len_loader,
                                  momentum_range, div_factor, increase_fraction)

        try:
            self.callbacks = Callbacks(callbacks.callbacks + [one_cycle_cb])

            self.kek(lr=max_lr,
                     epochs=cycle_len,
                     opt=opt,
                     opt_params=opt_params,
                     logdir=logdir,
                     cp_saver_params=cp_saver_params,
                     early_stop_params=early_stop_params)
        finally:
            # set old callbacks without OneCycle
            self.callbacks = callbacks

    def kek_lr(self,
               final_lr: float,
               logdir: Union[str, Path],
               init_lr: float = 1e-6,
               n_steps: Optional[int] = None) -> None:
        """Help you kek your model to the moon by finding "optimal" lr!

        Conducts the learning rate find procedure.
        For info please see:
        https://arxiv.org/abs/1803.09820
        https://sgugger.github.io/how-do-you-find-a-good-learning-rate.html

        Args:
            final_lr: the learning rate at the end of the lr find process
            logdir: the directory for tensorboard logs, that will allow you
                analyze the loss dynamic.
            init_lr: the learning rate at the start of the lr find process
            n_steps: the number of iterations of lr find process. If provided
                finding process will stop at this iteration, else lr find
                process will last one epoch
        """

        logdir = Path(logdir)
        logdir.mkdir(exist_ok=True)
        tmp_cp = logdir / "tmp.h5"
        self.save(tmp_cp)

        n_steps = n_steps or len(self._state.dataowner.train_dl)

        callbacks = self.callbacks

        try:
            lrfinder_cb = LRFinder(final_lr=final_lr,
                                   init_lr=init_lr,
                                   n_steps=n_steps)

            self.callbacks = Callbacks(self.core_callbacks + [lrfinder_cb])
            self.kek(lr=init_lr, epochs=1, skip_val=True, logdir=logdir)
        finally:
            self.callbacks = callbacks
            self.load(tmp_cp)
            tmp_cp.unlink()

    def _run_epoch(self,
                   epoch: int,
                   epochs: int) -> None:
        """Run one epoch of train-val procedure

        Args:
            epoch: number of the current epoch
            epochs: total number of epochs
        """
        self.callbacks.on_epoch_begin(epoch, epochs, self._state)

        with torch.set_grad_enabled(self.is_train):
            for i, batch in enumerate(self._state.loader):
                self.callbacks.on_batch_begin(i, self._state)

                self._state.batch = self.to_device(batch)

                self._state.out = self.step()

                self.callbacks.on_batch_end(i, self._state)

                if (self._state.stop_iter and self._state.mode == "train"
                        and i == self._state.stop_iter - 1):
                    # break only in train mode and if early stop is set
                    self._state.stop_epoch = True

                if self._state.stop_epoch:
                    self._state.stop_epoch = False
                    # st()
                    break

        self.callbacks.on_epoch_end(epoch, self._state)

        if self._state.checkpoint:
            self.save(self._state.checkpoint)
            self._state.checkpoint = ""

    def default_step(self) -> Dict[str, torch.Tensor]:
        """The default step function.
        Determine what your model will do with your data.

        Returns:
            the dict that contains prediction tensor for the batch.
        """
        inp = self._state.batch["image"]
        logits = self._state.model(inp)

        return {"logits": logits}

    def predict(self, savepath: Union[str, Path]) -> None:
        """Infer the model on test dataloader and saves prediction as numpy array

        Args:
            savepath: the directory to save predictions
        """
        callbacks = self.callbacks

        tmp_callbacks = Callbacks([ProgressBarCallback(),
                                   PredictionsSaverCallback(savepath,
                                                            self.preds_key)])

        self.callbacks = tmp_callbacks
        self.set_mode("test")
        with torch.set_grad_enabled(False):
            self._run_epoch(1, 1)

        self.callbacks = callbacks

    def predict_loader(self,
                       loader: DataLoader,
                       savepath: Union[str, Path]) -> None:
        """Infer the model on dataloader and saves prediction as numpy array

        Args:
            loader: the dataloader for generating predictions
            savepath: the directory to save predictions
        """
        callbacks = self.callbacks

        tmp_callbacks = Callbacks([ProgressBarCallback(),
                                   PredictionsSaverCallback(savepath,
                                                            self.preds_key)])

        self.callbacks = tmp_callbacks

        self._state.mode = "test"
        self._state.loader = loader
        self._state.model.eval()
        with torch.set_grad_enabled(False):
            self._run_epoch(1, 1)

        self.callbacks = callbacks

    def predict_tensor(self,
                       tensor: Type[torch.Tensor],
                       to_numpy: bool = False) -> Union[Type[torch.Tensor],
                                                        np.ndarray]:
        """Infer the model on one torch Tensor.

        Args:
            tensor: torch tensor to predict on.
                Should has [batch_size, *(one_sample_shape)] shape
            to_numpy: if True, converts predictions to numpy array

        Returns:
            Predictions on input tensor.
        """
        tensor = tensor.to(self.device)
        with torch.set_grad_enabled(False):
            self.set_mode("test")
            preds = self._state.model(tensor)
        if to_numpy:
            preds = preds.cpu().numpy()
        return preds

    def predict_array(self,
                      array: np.ndarray,
                      to_numpy: bool = False) -> Union[Type[torch.Tensor],
                                                       np.ndarray]:
        """Infer the model on one numpy array.

        Args:
            array: numpy array to predict on.
                Should has [batch_size, *(one_sample_shape)] shape
            to_numpy: if True, converts predictions to numpy array

        Returns:
            Predictions on input tensor.
        """
        tensor = torch.from_numpy(array)
        return self.predict_tensor(tensor, to_numpy)

    def TTA(self,
            loader: DataLoader,
            tfms: Union[List, Dict],
            savedir: Union[str, Path],
            prefix: str = "preds") -> None:
        """Conduct the test-time augmentations procedure.

        Create predictions for each set of provided transformations and saves
        each prediction in savedir as a numpy arrays.

        Args:
            loader: loader to predict
            tfms: the list with torchvision.transforms or
                  the dict with {"name": torchvision.transforms} pairs.
                  List indexes or dict keys will be used for generating
                  predictions names.
            savedir: the directory to save predictions
            prefix: the prefix for predictions files names
        """
        if isinstance(tfms, dict):
            names = [f"{prefix}_{k}.npy" for k in tfms]
            tfms = tfms.values()
        elif isinstance(tfms, list):
            names = [f"{prefix}_{i}.npy" for i in range(len(tfms))]
        else:
            raise ValueError(f"Transforms should be List or Dict, "
                             f"got {type(tfms)}")

        default_tfms = loader.dataset.transforms
        for name, tfm in zip(names, tfms):
            loader.dataset.transforms = tfm
            savepath = Path(savedir) / name
            self.predict_loader(loader, savepath)
        loader.dataset.transforms = default_tfms

    def save(self, savepath: Union[str, Path]) -> None:
        """Save models state dict on the specified path.

        Args:
            savepath: the path to save the state dict.
        """
        savepath = Path(savepath)
        savepath.parent.mkdir(exist_ok=True)
        torch.save(self._state.model.state_dict(), savepath)

    def load(self, loadpath: Union[str, Path]) -> None:
        """Loads models state dict from the specified path.

        Args:
            loadpath: the path from which the state dict will be loaded.
        """
        loadpath = Path(loadpath)
        checkpoint = torch.load(loadpath,
                                map_location=lambda storage, loc: storage)

        # workaround DataParallelModel
        if not isinstance(self._state.model, DataParallelModel) \
                and "module." in list(checkpoint.keys())[0]:
            # [7:] is to skip 'module.' in group name
            checkpoint = {k[7:]: v for k, v in checkpoint.items()}
        self._state.model.load_state_dict(checkpoint)

    def to_device(self,
                  batch: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """Moves tensors in batch to self.device.

        Args:
            batch: the batch dict.

        Returns:
            The batch dict with tensors on self.device.
        """
        return {k: v.to(self.device) for k, v in batch.items()
                if hasattr(v, "to")}

    def set_mode(self, mode: str) -> None:
        """Set the model to train or val and switch dataloaders

        Args:
            mode: 'train', 'val' or 'test', the mode of training procedure.
        """
        if mode == "train":
            self._state.model.train()
            self._state.loader = self._state.dataowner.train_dl
        elif mode == "val":
            self._state.model.eval()
            self._state.loader = self._state.dataowner.val_dl
        elif mode == "test":
            self._state.model.eval()
            self._state.loader = self._state.dataowner.test_dl
        self._state.mode = mode

    def freeze_to(self,
                  n: int,
                  freeze_bn: bool = False,
                  model_attr: Optional[str] = None) -> None:
        """Freeze model or model's part till specified layer.

        Args:
            n: the layer number to freeze to
            freeze_bn: if True batchnorm layers will be frozen too
            model_attr: the name of the model attribute if you want to specify
                when you want to freeze layers.
                For examples see example ipynb's.
        """

        module = self.get_model_attr(model_attr)
        freeze_to(module, n, freeze_bn)

    def freeze(self,
               freeze_bn: bool = False,
               model_attr: Optional[str] = None) -> None:
        """Freeze model or model's part till the last layer

        Args:
            freeze_bn: if True batchnorm layers will be frozen too
            model_attr: the name of the model attribute if you want to specify
                when you want to freeze layers.
                For examples see example ipynb's.
        """
        module = self.get_model_attr(model_attr)
        freeze(module, freeze_bn)

    def unfreeze(self,
                 model_attr: Optional[str] = None) -> None:
        """Unfreeze all model or model's part layers.

        Args:
            model_attr: the name of the model attribute if you want to specify
                when you want to freeze layers.
                For examples see example ipynb's.
        """
        module = self.get_model_attr(model_attr)
        unfreeze(module)

    def get_model_attr(self, model_attr: Union[str, None]) -> torch.nn.Module:
        """Get models attribute by name or return the model if name is None.

        Args:
            model_attr: models attribute name to get. If none, than the model
                will be returned.

        Returns:
            The models attribute or the model itself.
        """
        if self._state.parallel:
            model = self._state.model.module
        else:
            model = self._state.model

        if model_attr is not None:
            module = getattr(model, model_attr)
        else:
            module = model
        return module

    @property
    def is_train(self) -> bool:
        return self._state.mode == "train"
