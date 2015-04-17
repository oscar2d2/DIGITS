# Copyright (c) 2014-2015, NVIDIA CORPORATION.  All rights reserved.

import os
import re
import caffe
import time
import math
import subprocess

import numpy as np

import tempfile
import PIL.Image
import digits
from train import TrainTask
from digits.config import config_option
from digits.status import Status
from digits import utils, dataset
from digits.utils import subclass, override, constants
from digits.dataset import ImageClassificationDatasetJob

# NOTE: Increment this everytime the pickled object changes
PICKLE_VERSION = 1

@subclass
class TorchTrainTask(TrainTask):
    """
    Trains a torch model
    """

    TORCH_LOG = 'torch_output.log'

    def __init__(self, **kwargs):
        """
        Arguments:
        network -- a NetParameter defining the network
        """
        super(TorchTrainTask, self).__init__(**kwargs)
        self.pickver_task_torch_train = PICKLE_VERSION

        #self.network = network

        self.current_epoch = 0

        self.loaded_snapshot_file = None
        self.loaded_snapshot_epoch = None
        self.image_mean = None
        self.classifier = None
        self.solver = None

        self.model_file = constants.TORCH_MODEL_FILE
        self.train_file = constants.TRAIN_DB
        self.val_file = constants.VAL_DB
        self.snapshot_prefix = constants.TORCH_SNAPSHOT_PREFIX
        self.log_file = self.TORCH_LOG

    def __getstate__(self):
        state = super(TorchTrainTask, self).__getstate__()

        # Don't pickle these things
        if 'labels' in state:
            del state['labels']
        if 'image_mean' in state:
            del state['image_mean']
        if 'classifier' in state:
            del state['classifier']
        if 'torch_log' in state:
            del state['torch_log']

        return state

    def __setstate__(self, state):
        super(TorchTrainTask, self).__setstate__(state)

        # Make changes to self
        self.loaded_snapshot_file = None
        self.loaded_snapshot_epoch = None

        # These things don't get pickled
        self.image_mean = None
        self.classifier = None

    ### Task overrides

    @override
    def name(self):
        return 'Train Torch Model'

    @override
    def framework_name(self):
        return 'torch'

    @override
    def before_run(self):
        # TODO
        if not isinstance(self.dataset, dataset.ImageClassificationDatasetJob):
            raise NotImplementedError()

        self.torch_log = open(self.path(self.TORCH_LOG), 'a')
        self.saving_snapshot = False
        self.receiving_train_output = False
        self.receiving_val_output = False
        self.last_train_update = None
        return True

    @override
    def task_arguments(self, **kwargs):
        # TODO
        gpu_id = kwargs.pop('gpu_id', None)

        #args = [os.path.join(config_option('caffe_root'), 'bin', 'caffe.bin'),
        if config_option('torch_root') == 'SYS':
            torch_bin = 'th'
        else:
            torch_bin = os.path.join(config_option('torch_root'), 'th')

        if self.batch_size is None:
            self.batch_size = constants.DEFAULT_TORCH_BATCH_SIZE

        args = [torch_bin,
                os.path.join(os.path.dirname(os.path.dirname(digits.__file__)),'tools','torch','main.lua'),
                '--network=%s' % self.model_file.split(".")[0],
                '--epoch=%d' % int(self.train_epochs),
                '--train=%s' % self.dataset.path(constants.TRAIN_DB),
                '--networkDirectory=%s' % self.job_dir,
                '--save=%s' % self.job_dir,
                '--snapshotPrefix=%s' % self.snapshot_prefix,
                '--snapshotInterval=%f' % self.snapshot_interval,
                #'--shuffle=yes',
                '--useMeanPixel=yes',
                '--mean=%s' % self.dataset.path(constants.MEAN_FILE_IMAGE),
                '--labels=%s' % self.dataset.path(self.dataset.labels_file),
                '--batchSize=%d' % self.batch_size,
                '--learningRate=%f' % self.learning_rate,
                '--policy=%s' % str(self.lr_policy['policy'])
                ]

        #learning rate policy input parameters
        if self.lr_policy['policy'] == 'fixed':
            pass
        elif self.lr_policy['policy'] == 'step':
            args.append('--gamma=%f' % self.lr_policy['gamma'])
            args.append('--stepvalues=%f' % self.lr_policy['stepsize'])
        elif self.lr_policy['policy'] == 'multistep':
            args.append('--stepvalues=%s' % self.lr_policy['stepvalue'])
            args.append('--gamma=%f' % self.lr_policy['gamma'])
        elif self.lr_policy['policy'] == 'exp':
            args.append('--gamma=%f' % self.lr_policy['gamma'])
        elif self.lr_policy['policy'] == 'inv':
            args.append('--gamma=%f' % self.lr_policy['gamma'])
            args.append('--power=%f' % self.lr_policy['power'])
        elif self.lr_policy['policy'] == 'poly':
            args.append('--power=%f' % self.lr_policy['power'])
        elif self.lr_policy['policy'] == 'sigmoid':
            args.append('--stepvalues=%f' % self.lr_policy['stepsize'])
            args.append('--gamma=%f' % self.lr_policy['gamma'])

        if self.crop_size:
            args.append('--crop=yes')
            args.append('--croplen=%d' % self.crop_size)

        if self.use_mean:
            args.append('--subtractMean=yes')
        else:
            args.append('--subtractMean=no')

        if os.path.exists(self.dataset.path(constants.VAL_DB)) and self.val_interval > 0:
            args.append('--validation=%s' % self.dataset.path(constants.VAL_DB))
            args.append('--interval=%f' % self.val_interval)

        if gpu_id:
            args.append('--devid=%d' % (gpu_id+1))

        #if self.pretrained_model:
        #    args.append('--weights=%s' % self.path(self.pretrained_model))
        #print args
        return args

    @override
    def process_output(self, line):
        from digits.webapp import socketio
        regex = re.compile('\x1b\[[0-9;]*m', re.UNICODE)   #TODO: need to include regular expression for MAC color codes
        line=regex.sub('', line).strip()
        self.torch_log.write('%s\n' % line)
        self.torch_log.flush()

        # parse caffe header
        timestamp, level, message = self.preprocess_output_torch(line)

        if not message:
            return True

        float_exp = '([-]?inf|[-+]?[0-9]*\.?[0-9]+(e[-+]?[0-9]+)?)'

        # loss and learning rate updates
        match = re.match(r'Training \(epoch (\d+\.?\d*)\): \w*loss\w* = %s, lr = %s'  % (float_exp, float_exp), message)
        if match:
            index = float(match.group(1))
            l = match.group(2)
            assert l.lower() != '-inf', 'Network reported -inf for training loss. Try changing your learning rate.'       #TODO: messages needs to be corrected
            assert l.lower() != 'inf', 'Network reported inf for training loss. Try decreasing your learning rate.'
            l = float(l)
            lr = match.group(4)
            assert lr.lower() != '-inf', 'Network reported -inf for learning rate. Try changing your learning rate.'
            assert lr.lower() != 'inf', 'Network reported inf for learning rate. Try decreasing your learning rate.'
            lr = float(lr)
            # epoch updates
            self.send_progress_update(index)

            self.save_train_output('loss', 'SoftmaxWithLoss', l)
            self.save_train_output('learning_rate', 'LearningRate', lr)
            self.logger.debug(message)

            return True

        # testing loss and accuracy updates
        match = re.match(r'Validation \(epoch (\d+\.?\d*)\): \w*loss\w* = %s, accuracy = %s' % (float_exp,float_exp), message, flags=re.IGNORECASE)
        if match:
            index = float(match.group(1))
            l = match.group(2)
            a = match.group(4)
            if l.lower() != 'inf' and l.lower() != '-inf' and a.lower() != 'inf' and a.lower() != '-inf':
                l = float(l)
                a = float(a)
                self.logger.debug('Network accuracy #%s: %s' % (index, a))
                # epoch updates
                self.send_progress_update(index)

                self.save_val_output('loss', 'SoftmaxWithLoss', l)
                self.save_val_output('accuracy', 'Accuracy', a)

            return True

        # snapshot saved
        if self.saving_snapshot:
            if not message.startswith('Snapshot saved'):
                self.logger.warning('Torch output format seems to have changed. Expected "Snapshot saved..." after "Snapshotting to..."')
            else:
                self.logger.info('Snapshot saved.')  # to print file name here, you can use "message"
            self.detect_snapshots()
            self.send_snapshot_update()
            self.saving_snapshot = False
            return True

        # snapshot starting
        match = re.match(r'Snapshotting to (.*)\s*$', message)
        if match:
            self.saving_snapshot = True
            return True

        if level in ['error', 'critical']:
            self.logger.error('%s: %s' % (self.name(), message))
            self.exception = message
            return True
        return True

    def preprocess_output_torch(self, line):
        """
        Takes line of output and parses it according to caffe's output format
        Returns (timestamp, level, message) or (None, None, None)
        """
        # NOTE: This must change when the logging format changes
        # LMMDD HH:MM:SS.MICROS pid file:lineno] message
        match = re.match(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s\[(\w+)\s*]\s+(\S.*)$', line)
        if match:
            timestamp = time.mktime(time.strptime(match.group(1), '%Y-%m-%d %H:%M:%S'))
            level = match.group(2)
            message = match.group(3)
            if level == 'INFO':
                level = 'info'
            elif level == 'WARNING':
                level = 'warning'
            elif level == 'ERROR':
                level = 'error'
            elif level == 'FAIL': #FAIL
                level = 'critical'
            return (timestamp, level, message)
        else:
            #self.logger.warning('Unrecognized task output "%s"' % line)
            return (None, None, None)

    def send_snapshot_update(self):
        """
        Sends socketio message about the snapshot list
        """
        # TODO: move to TrainTask
        from digits.webapp import socketio

        socketio.emit('task update',
                {
                    'task': self.html_id(),
                    'update': 'snapshots',
                    'data': self.snapshot_list(),
                    },
                namespace='/jobs',
                room=self.job_id,
                )

    ### TrainTask overrides
    @override
    def after_run(self):
        self.torch_log.close()

    @override
    def after_runtime_error(self):
        if os.path.exists(self.path(self.TORCH_LOG)):
            output = subprocess.check_output(['tail', '-n40', self.path(self.TORCH_LOG)])
            lines = []
            for line in output.split('\n'):
                # parse torch header
                timestamp, level, message = self.preprocess_output_torch(line)

                if message:
                    lines.append(message)
            # return the last 20 lines
            self.traceback = '\n'.join(lines[len(lines)-20:])

    @override
    def detect_snapshots(self):
        # TODO
        self.snapshots = []

        snapshot_dir = os.path.join(self.job_dir, os.path.dirname(self.snapshot_prefix))
        snapshots = []
        solverstates = []

        for filename in os.listdir(snapshot_dir):
            # find models
            match = re.match(r'%s_(\d+)\.?(\d*)_Weights\.t7' % os.path.basename(self.snapshot_prefix), filename)
            if match:
                epoch = 0
                if match.group(2) == '':
                    epoch = int(match.group(1))
                else:
                    epoch = float(match.group(1) + '.' + match.group(2))
                snapshots.append( (
                        os.path.join(snapshot_dir, filename),
                        epoch
                        )
                    )

        self.snapshots = sorted(snapshots, key=lambda tup: tup[1])

        return len(self.snapshots) > 0

    @override
    def est_next_snapshot(self):
        # TODO: Currently this function is not in use. Probably in future we may have to implement this
        return None

    @override
    def can_view_weights(self):
        return False

    @override
    def can_infer_one(self):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return True
        return False

    @override
    def infer_one(self, data, snapshot_epoch=None, layers=None):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return self.classify_one(data,
                    snapshot_epoch=snapshot_epoch,
                    layers=layers,
                    )
        raise NotImplementedError()

    def classify_one(self, image, snapshot_epoch=None, layers=None):
        """
        Classify an image
        Returns (predictions, visualizations)
            predictions -- an array of [ (label, confidence), ...] for each label, sorted by confidence
            visualizations -- an array of (layer_name, activations, weights) for the specified layers
        Returns (None, None) if something goes wrong

        Arguments:
        image -- a np.array

        Keyword arguments:
        snapshot_epoch -- which snapshot to use
        layers -- which layer activation[s] and weight[s] to visualize
        """
        _, temp_image_path = tempfile.mkstemp(suffix='.jpeg')
        image = PIL.Image.fromarray(image)
        try:
            image.save(temp_image_path, format='jpeg')
        except KeyError:
            self.logger.error('Unable to save file to "%s"' % temp_image_path)
            return (None,None)   # TODO fix this error message

        if config_option('torch_root') == 'SYS':
            torch_bin = 'th'
        else:
            torch_bin = os.path.join(config_option('torch_root'), 'th')

        args = [torch_bin,
                os.path.join(os.path.dirname(os.path.dirname(digits.__file__)),'tools','torch','test.lua'),
		'--image=%s' % temp_image_path,
                '--network=%s' % self.model_file.split(".")[0],
                '--epoch=%d' % int(snapshot_epoch),
                '--networkDirectory=%s' % self.job_dir,
                '--load=%s' % self.job_dir,
                '--snapshotPrefix=%s' % self.snapshot_prefix,
                '--mean=%s' % self.dataset.path(constants.MEAN_FILE_IMAGE),
                '--labels=%s' % self.dataset.path(self.dataset.labels_file)
                ]

        if constants.TORCH_USE_MEAN_PIXEL:
            args.append('--useMeanPixel=yes')

        if self.crop_size:
            args.append('--crop=yes')
            args.append('--croplen=%d' % self.crop_size)

        if self.use_mean:
            args.append('--subtractMean=yes')
        else:
            args.append('--subtractMean=no')

        #print args

        # Convert them all to strings
        args = [str(x) for x in args]

        self.logger.info('%s test task started.' % self.name())
        self.status = Status.RUN

        unrecognized_output = []
	predictions = []
        p = subprocess.Popen(args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.job_dir,
                close_fds=True,
                )

        try:
            while p.poll() is None:
                for line in utils.nonblocking_readlines(p.stdout):
                    if self.aborted.is_set():
                        p.terminate()
                        self.status = Status.ABORT
                        break

                    if line is not None:
                        # Remove whitespace
                        line = line.strip()
                    if line:
                        if not self.process_test_output(line, predictions):
                            self.logger.warning('%s unrecognized input: %s' % (self.name(), line.strip()))
                            unrecognized_output.append(line)
                    else:
                        time.sleep(0.05)
        except Exception as e:
            p.terminate()
            pass

        if p.returncode != 0:
            self.logger.error('%s task failed with error code %d' % (self.name(), p.returncode))
            if self.exception is None:
                self.exception = 'error code %d' % p.returncode
                if unrecognized_output:
                    self.traceback = '\n'.join(unrecognized_output)
            #self.after_test_run(temp_image_path)
            self.status = Status.ERROR
            return (None,None)
        else:
            self.logger.info('%s test task completed.' % self.name())
            #self.after_test_run(temp_image_path)
            self.status = Status.DONE

        #if gpu_id:
        #    args.append('--devid=%d' % (gpu_id+1))
	return (predictions,None)

    def after_test_run(self, temp_image_path):
        try:
            os.remove(temp_image_path)
        except OSError:
            pass

    def process_test_output(self, line, predictions):
        #from digits.webapp import socketio
        regex = re.compile('\x1b\[[0-9;]*m', re.UNICODE)   #TODO: need to include regular expression for MAC color codes
        line=regex.sub('', line).strip()

        # parse caffe header
        timestamp, level, message = self.preprocess_output_torch(line)

        if not message:
            return True

        float_exp = '([-]?inf|[-+]?[0-9]*\.?[0-9]+(e[-+]?[0-9]+)?)'

        # loss and learning rate updates
        match = re.match(r'Predicted class \d+: (\d+) \(.*?\) %s'  % (float_exp), message)
        if match:
            label = int(match.group(1))
            confidence = match.group(2)
            assert confidence.lower() != 'nan', 'Network reported "nan" for confidence value. Please check image and network'
            confidence = float(confidence)
            predictions.append((label-1, confidence))   # In Torch, array index starts from 1 instead of 0. So, subtracted 1 from label value to refer correct label in labels file.
            return True

        if level in ['error', 'critical']:
            self.logger.error('%s: %s' % (self.name(), message))
            self.exception = message
            return True
        return True

    @override
    def can_infer_many(self):
        return False

    def has_model(self):
        """
        Returns True if there is a model that can be used
        """
        return len(self.snapshots) != 0

    def loaded_model(self):
        """
        Returns True if a model has been loaded
        """
        return None

    def load_model(self, epoch=None):
        """
        Loads a .caffemodel
        Returns True if the model is loaded (or if it was already loaded)

        Keyword Arguments:
        epoch -- which snapshot to load (default is -1 to load the most recently generated snapshot)
        """
        return False

