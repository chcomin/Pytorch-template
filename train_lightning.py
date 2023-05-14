from pathlib import Path
import torch
from torch import optim, nn
import lightning.pytorch as pl
from lightning.pytorch.callbacks import ModelCheckpoint, LearningRateMonitor
from lightning.pytorch.loggers import TensorBoardLogger
import torchtrainer   #https://github.com/chcomin/torchtrainer
from dataset import create_datasets

class LitSeg(pl.LightningModule):
    def __init__(self, model_layers, model_channels, loss, class_weights, lr, momentum, weight_decay, iters):
        super().__init__()
        self.save_hyperparameters()

        # Define loss function
        if loss=='cross_entropy':
            loss_func = nn.CrossEntropyLoss(weight=torch.tensor(class_weights, device=self.device)) 
        elif loss=='label_weighted_cross_entropy':
            loss_func = torchtrainer.perf_funcs.LabelWeightedCrossEntropyLoss()

        # Model
        model = torchtrainer.models.resnet_seg.ResNetSeg(model_layers, model_channels)

        self.loss_func = loss_func
        self.learnin_rate = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.iters = iters
        self.model = model

    def forward(self, x):
        return self.model(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
        output = self.model(x)
        loss = self.loss_func(output, y)

        self.log("train_loss", loss, prog_bar=True)
        return loss
    
    def validation_step(self, batch, batch_idx):
        x, y = batch
        output = self.model(x)
        loss = self.loss_func(output, y)
        acc = torchtrainer.perf_funcs.segmentation_accuracy(output, y)

        self.log("val_loss", loss, prog_bar=True)       
        self.log_dict(acc)   
        self.log("hp_metric", loss)   # Metric to show with hyperparameters in Tensorboard

    def configure_optimizers(self):
        optimizer = optim.SGD(self.parameters(), lr=self.learnin_rate, momentum=self.momentum, weight_decay=self.weight_decay)
        lr_scheduler = torch.optim.lr_scheduler.PolynomialLR(optimizer, total_iters=self.iters, power=0.9)
        lr_scheduler_config = {
            "scheduler": lr_scheduler,
            "interval": "step",
        }

        return {'optimizer':optimizer, 'lr_scheduler':lr_scheduler_config}
    
    #def on_train_start(self):
    #    self.logger.log_hyperparams(self.hparams, {"val_loss": 0, "iou": 0})

def run(params):

    # Mixed precision
    if params['use_amp']:
        precision = '16-mixed'
    else:
        precision = '32-true'
    seed = params['seed']
    if seed is not None:
        # workers=True sets different seeds for each worker.
        pl.seed_everything(seed, workers=True)

    # Create dataset and datalaoders
    ds_train, ds_valid, _ = create_datasets(params['img_dir'], params['label_dir'], params['crop_size'], params['train_val_split'], use_simple=not params['use_transforms'])
    data_loader_train = torch.utils.data.DataLoader(
        ds_train,
        batch_size=params['batch_size'],
        shuffle=True,
        num_workers=params['num_workers'],
        pin_memory=params['pin_memory'],
        persistent_workers=params['num_workers']>0   # Avoid recreating workers at each epoch
    )

    data_loader_valid = torch.utils.data.DataLoader(
        ds_valid,
        batch_size=1,     # TODO: Include parameter for validation batch size. It is complicated because images are larger during validation
        shuffle=False,
        num_workers=params['num_workers'],
        pin_memory=params['pin_memory'],
        persistent_workers=params['num_workers']>0   # Avoid recreating workers at each epoch
    )
    total_iters = len(data_loader_train)*params['epochs']

    # Folder for saving logs
    experiment_folder = Path(params['log_dir'])/f'version_{params["version"]}'
    experiment_folder.mkdir(parents=True, exist_ok=True)

    if params['resume']:
        # Resume previous experiment
        checkpoint_file = experiment_folder/'checkpoints/last.ckpt'
        lit_model = LitSeg.load_from_checkpoint(checkpoint_file) 
        start_epoch = lit_model.current_epoch + 1
        if seed is not None:
            # Seed using the current epoch to avoid using the same seed as in epoch 0 when resuming
            pl.seed_everything(seed+start_epoch, workers=True)
    else:
        checkpoint_file = None
        lit_model = LitSeg(params['model_layers'], params['model_channels'], params['loss'], params['class_weights'], params['lr'], params['momentum'], params['weight_decay'], total_iters)
        start_epoch = 0

    callbacks = [LearningRateMonitor()]
    if params['save_best']:
        # Create callback for saving model with best validation loss
        checkpoint_loss = ModelCheckpoint(save_top_k=1, monitor="val_loss", mode="min", 
                                            filename="best_val_loss-{epoch:02d}-{val_loss:.2f}")
        callbacks.append(checkpoint_loss)
    # Callback for saving the model at the end of each epoch
    callbacks.append(ModelCheckpoint(save_last=True))

    logger = TensorBoardLogger('.', name=params['log_dir'], version=params['version'])

    trainer = pl.Trainer(max_epochs=start_epoch+params['epochs'], callbacks=callbacks, precision=precision, logger=logger)
    trainer.fit(lit_model, data_loader_train, data_loader_valid, ckpt_path=checkpoint_file)

    return ds_train, ds_valid, lit_model, trainer
