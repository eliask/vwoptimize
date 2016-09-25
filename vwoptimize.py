#!/usr/bin/env python
"""Wrapper for Vowpal Wabbit that does cross-validation and hyper-parameter tuning"""
import sys
import os
import optparse
import traceback
import math
import csv
import re
import subprocess
import random
import time
from pipes import quote
import numpy as np


csv.field_size_limit(10000000)
LOG_LEVEL = 1
MAIN_PID = str(os.getpid())
vowpal_wabbit_error_messages = ['error', "won't work right", 'errno', "can't open", 'VW::vw_exception']


for path in '.vwoptimize /tmp/vwoptimize'.split():
    if os.path.exists(path):
        TMP_PREFIX = path
        break
    try:
        os.mkdir(path)
    except Exception, ex:
        sys.stderr.write('Failed to create %r: %s\n' % (path, ex))
    else:
        TMP_PREFIX = path
        break


def htmlparser_unescape(text, cache=[]):
    if not cache:
        import HTMLParser
        cache.append(HTMLParser.HTMLParser())
    return cache[0].unescape(text)


def unlink(*filenames):
    for filename in filenames:
        if not filename:
            continue
        if not isinstance(filename, basestring):
            sys.exit('unlink() expects list of strings: %r\n' % (filenames, ))
        if not os.path.exists(filename):
            continue
        try:
            os.unlink(filename)
        except Exception:
            sys.stderr.write('Failed to unlink %r\n' % filename)
            traceback.print_exc()


def kill(*jobs, **kwargs):
    verbose = kwargs.pop('verbose', False)
    assert not kwargs, kwargs
    for job in jobs:
        try:
            if job.poll() is None:
                if verbose:
                    log('Killing %s', job.pid)
                job.kill()
        except Exception, ex:
            if 'no such process' not in str(ex):
                sys.stderr.write('Failed to kill %r: %s\n' % (job, ex))


def open_regular_or_compressed(filename):
    if hasattr(filename, 'read'):
        fobj = filename
    else:
        f = filename.lower()
        ext = f.rsplit('.', 1)[-1]
        if ext == 'gz':
            import gzip
            fobj = gzip.GzipFile(filename)
        elif ext == 'bz2':
            import bz2
            fobj = bz2.BZ2File(filename)
        elif ext == 'xz':
            import lzma
            fobj = lzma.open(filename)
        else:
            fobj = open(filename)
    return fobj


def get_real_ext(filename):
    filename = filename.rsplit('/', 1)[-1]
    items = filename.rsplit('.', 2)
    if len(items) >= 2 and items[-1] in 'gz bz2 xz'.split():
        return items[-2]
    return items[-1]


def get_temp_filename(suffix, counter=[0]):
    counter[0] += 1
    fname = '%s/%s.%s.%s' % (TMP_PREFIX, MAIN_PID, counter[0], suffix)
    assert not os.path.exists(fname), 'internal error: %s' % fname
    return fname


def log(s, *params, **kwargs):
    log_level = int(kwargs.pop('log_level', None) or 0)
    assert not kwargs, kwargs
    if log_level >= LOG_LEVEL:
        sys.stdout.flush()
        try:
            s = s % params
        except Exception:
            s = '%s %r' % (s, params)
        sys.stderr.write('%s\n' % (s, ))


def guess_format_from_contents(lines):
    count_pipes = set([x.count('|') for x in lines if x.strip(' \r\n')])
    count_tabs = set([x.count('\t') for x in lines if x.strip(' \r\n')])
    count_commas = set([x.count(',') for x in lines if x.strip(' \r\n')])

    possible_formats = []

    if count_pipes and 0 not in count_pipes:
        possible_formats.append('vw')

    if count_tabs and 0 not in count_tabs and len(count_tabs) == 1:
        possible_formats.append('tsv')

    if count_commas and 0 not in count_commas and len(count_commas) == 1:
        possible_formats.append('csv')

    if len(possible_formats) == 1:
        return possible_formats[0]

    if count_commas and 0 not in count_commas:
        return 'csv'


def guess_format_from_filename(filename):
    items = filename.lower().split('.')

    for ext in reversed(items):
        if ext in ['vw', 'csv', 'tsv']:
            return ext

    log('Could not guess format of %s from filename, readine first 10 lines', filename)
    fobj = open_regular_or_compressed(filename)
    lines = [fobj.readline() for _ in xrange(10)]

    return guess_format_from_contents(lines)


def guess_format(filename):
    result = None

    if hasattr(filename, 'lower'):
        result = guess_format_from_filename(filename)

    elif hasattr(filename, 'getvalue'):
        peek = [x for x in filename.getvalue().split('\n', 10)]
        if len(peek) >= 10:
            peek = peek[:-1]
        result = guess_format_from_contents(peek)

    if result is not None:
        return result

    sys.exit('Cound not guess format from %s, provide --format vw|tsv|csv' % filename)


def _read_lines_vw(fobj):
    for orig_line in fobj:
        line = orig_line.strip()
        if not line:
            continue
        items = line.split()
        klass = items[0]
        if klass.startswith("'"):
            yield (None, orig_line)
        else:
            yield (klass, orig_line)


def _read_lines_csv(reader):
    expected_columns = None
    errors = 0

    for row in reader:
        if not row:
            continue

        bad_line = False

        if len(row) <= 1:
            bad_line = 'Only one column'
        elif expected_columns is not None and len(row) != expected_columns:
            bad_line = 'Expected %s columns, got %s' % (expected_columns, len(row))

        if bad_line:
            log('Bad line (%s): %s', bad_line, limited_repr(row), log_level=3)
            errors += 1
            if errors >= 10:
                sys.exit('Too many errors while reading %s' % reader)
            continue

        errors = 0
        expected_columns = len(row)
        klass = row[0]
        klass = klass.replace(',', '_')
        yield row


def open_anything(source, format):
    if format == 'vw':
        return _read_lines_vw(open_regular_or_compressed(source))

    if format == 'tsv':
        reader = csv.reader(open_regular_or_compressed(source), csv.excel_tab)
    elif format == 'csv':
        reader = csv.reader(open_regular_or_compressed(source), csv.excel)
    else:
        raise ValueError('format not supported: %s' % format)

    return _read_lines_csv(reader)


def limited_repr(obj, limit=80):
    s = repr(obj)
    if len(s) >= limit:
        s = s[:limit - 3] + '...'
    return s


class PassThroughOptionParser(optparse.OptionParser):

    def _process_args(self, largs, rargs, values):
        while rargs:
            try:
                optparse.OptionParser._process_args(self, largs, rargs, values)
            except (optparse.BadOptionError, optparse.AmbiguousOptionError), e:
                largs.append(e.opt_str)


def system(cmd, log_level=0):
    sys.stdout.flush()
    start = time.time()
    log('+ %s' % cmd, log_level=log_level)
    retcode = os.system(cmd)
    if retcode:
        log('%s [%.1fs] %s', '-' if retcode == 0 else '!', time.time() - start, cmd, log_level=log_level - 1)
    if retcode:
        sys.exit(1)


def split_file(source, nfolds=None, limit=None, shuffle=False):
    if nfolds is None:
        nfolds = 10

    # XXX must use open_anything to support csv files with headers

    if isinstance(source, basestring):
        ext = get_real_ext(source)
    else:
        ext = 'xxx'

    if shuffle:
        lines = open_regular_or_compressed(source).readlines()
        total_lines = len(lines)
        random.shuffle(lines)
        source = lines
    else:
        # XXX already have examples_count
        total_lines = 0
        for line in open_regular_or_compressed(source):
            total_lines += 1

        if hasattr(source, 'seek'):
            source.seek(0)

        source = open_regular_or_compressed(source)

    if limit is not None:
        total_lines = min(total_lines, limit)

    foldsize = int(math.ceil(total_lines / float(nfolds)))
    foldsize = max(foldsize, 1)
    nfolds = int(math.ceil(total_lines / float(foldsize)))
    folds = []

    current_fold = -1
    count = foldsize
    current_fileobj = None
    total_count = 0
    for line in source:
        if count >= foldsize:
            if current_fileobj is not None:
                current_fileobj.flush()
                os.fsync(current_fileobj.fileno())
                current_fileobj.close()
                current_fileobj = None
            current_fold += 1
            if current_fold >= nfolds:
                break
            fname = get_temp_filename('fold%s.%s' % (current_fold, ext))
            current_fileobj = open(fname, 'w')
            count = 0
            folds.append(fname)
        current_fileobj.write(line)
        count += 1
        total_count += 1

    if current_fileobj is not None:
        current_fileobj.flush()
        os.fsync(current_fileobj.fileno())
        current_fileobj.close()

    if total_count != total_lines:
        sys.exit('internal error: total_count=%r total_lines=%r source=%r' % (total_count, total_lines, source))

    return folds, total_lines


def _workers(workers):
    if workers is not None and workers <= 1:
        return 1
    if workers is None or workers <= 0:
        import multiprocessing
        return multiprocessing.cpu_count()
    return workers


def die_if_parent_dies(signum=9):
    if 'linux' not in sys.platform:
        return
    try:
        import ctypes
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        PR_SET_PDEATHSIG = 1
        result = libc.prctl(PR_SET_PDEATHSIG, signum)
        if result == 0:
            return True
        else:
            log('prctl failed: %s', os.strerror(ctypes.get_errno()))
    except StandardError, ex:
        sys.stderr.write(str(ex) + '\n')


def run_subprocesses(cmds, workers=None, log_level=None):
    workers = _workers(workers)
    cmds_queue = list(cmds)
    cmds_queue.reverse()
    queue = []
    while queue or cmds_queue:
        if cmds_queue and len(queue) <= workers:
            cmd = cmds_queue.pop()
            log('+ %s', cmd, log_level=log_level)
            popen = subprocess.Popen(cmd, shell=True, preexec_fn=die_if_parent_dies)
            popen._cmd = cmd
            queue.append(popen)
        else:
            popen = queue[0]
            del queue[0]
            retcode = popen.wait()
            if retcode:
                log('failed: %s', popen._cmd, log_level=3)
                kill(*queue, verbose=True)
                return False
            else:
                log('%s %s', '-' if retcode == 0 else '!', popen._cmd, log_level=log_level)

    return True


def vw_cross_validation(folds, vw_args, workers=None, p_fname=None, r_fname=None, audit=False):
    assert len(folds) >= 2, folds
    workers = _workers(workers)
    p_filenames = []
    r_filenames = []
    audit_filenames = []
    training_commands = []
    testing_commands = []
    outputs = []
    to_cleanup = []

    if '--quiet' not in vw_args:
        vw_args += ' --quiet'

    for test_fold in xrange(len(folds)):
        trainset = [fold for (index, fold) in enumerate(folds) if index != test_fold]
        assert trainset and os.path.exists(trainset[0]), trainset
        trainset = ' '.join(trainset)
        testset = folds[test_fold]
        assert testset and os.path.exists(testset), testset

        model_filename = get_temp_filename('model')
        to_cleanup.append(model_filename)

        p_filename = '%s.predictions' % model_filename
        with_p = '-p %s' % p_filename
        p_filenames.append(p_filename)

        if r_fname:
            r_filename = '%s.raw' % model_filename
            with_r = '-r %s' % r_filename
            r_filenames.append(r_filename)
        else:
            r_filename = None
            with_r = ''

        my_args = vw_args

        cache_file = None
        if '-c' in my_args.split() and '--cache_file' not in my_args:
            my_args = my_args.replace('-c', '')
            cache_file = get_temp_filename('cache')
            my_args += ' --cache_file %s' % cache_file
            to_cleanup.append(cache_file)

        train_capture = get_temp_filename('train_capture')
        test_capture = get_temp_filename('test_capture')
        outputs.append(train_capture)
        outputs.append(test_capture)

        if audit:
            audit_filename = '%s.audit' % model_filename
            audit = '-a > %s' % audit_filename
            capture = '2> %s' % test_capture
            audit_filenames.append(audit_filename)
        else:
            audit_filename = ''
            audit = ''
            capture = '&> %s' % test_capture

        training_command = 'cat %s | vw -f %s %s &> %s' % (trainset, model_filename, my_args, train_capture)
        testing_command = 'vw --quiet -d %s -t -i %s %s %s %s %s' % (testset, model_filename, with_p, with_r, audit, capture)
        training_commands.append(training_command)
        testing_commands.append(testing_command)

    has_error = False

    try:
        success = run_subprocesses(training_commands, workers=workers, log_level=-1)

        if success:
            success = run_subprocesses(testing_commands, workers=workers, log_level=-1)

        for output in outputs:
            if output and os.path.exists(output):
                has_error = print_vw_output(open(output))
                if has_error:
                    break

        if has_error:
            sys.exit('vw failed: %s' % has_error)

        if not success:
            sys.exit('vw failed')

        predictions = []
        for fname in p_filenames:
            predictions.extend(_load_first_float_from_each_string(fname))

        if p_fname and p_filenames:
            system('cat %s > %s' % (' '.join(p_filenames), p_fname), log_level=-1)

        if r_fname and r_filenames:
            system('cat %s > %s' % (' '.join(r_filenames), r_fname), log_level=-1)

        return np.array(predictions)

    finally:
        unlink(*p_filenames)
        unlink(*r_filenames)
        unlink(*audit_filenames)
        unlink(*to_cleanup)
        unlink(*outputs)


def print_vw_output(lines):
    prev_line = None
    has_error = False
    for line in lines:
        if not line.strip():
            continue
        if line == prev_line:
            continue
        sys.stderr.write(line.rstrip() + '\n')
        msg = line.lower()
        if not has_error:
            for errmsg in vowpal_wabbit_error_messages:
                if errmsg in msg:
                    has_error = line.strip()
        prev_line = line
    return has_error


def _load_first_float_from_each_string(file):
    filename = file
    if hasattr(file, 'read'):
        pass
    elif isinstance(file, basestring):
        file = open(file)
    else:
        return file

    result = []

    for line in file:
        try:
            result.append(float(line.split()[0]))
        except:
            sys.stderr.write('Error while parsing %r\nin %r\n' % (line, filename))
            raise

    return result


class BaseParam(object):

    PRINTABLE_KEYS = 'opt init min max values format extra'.split()
    _cast = float

    @classmethod
    def cast(cls, value):
        if value is None:
            return None
        if value == '':
            return None
        return cls._cast(value)

    def pack(self, value):
        if self._pack is None:
            return value
        return self._pack(value)

    def unpack(self, value):
        if self._unpack is None:
            return value
        return self._unpack(value)

    def __init__(self, opt, init=None, min=None, max=None, format=None, pack=None, unpack=None, extra=None):
        self.opt = opt
        self.init = self.cast(init)
        self.min = self.cast(min)
        self.max = self.cast(max)
        self.format = format
        self._pack = pack
        self._unpack = unpack
        self.extra = None

        if self.init is None:
            if self.min is not None and self.max is not None:
                self.init = self.avg(self.min, self.max)
            elif self.min is not None:
                self.init = self.min
            elif self.max is not None:
                self.init = self.max

    def avg(self, a, b):
        result = self.cast(self.unpack((self.pack(self.min) + self.pack(self.max)) / 2.0))
        if self.format:
            result = self.format % result
            result = self.cast(result)
        return result

    def __repr__(self):
        klass = type(self).__name__
        items = []
        for name in self.PRINTABLE_KEYS:
            value = getattr(self, name, None)
            if value is not None:
                items.append('%s=%r' % (name, value))
        return klass + '(' + ', '.join(items) + ')'

    def packed_init(self):
        init = self.init
        init = self.pack(init)
        return init

    def get_extra_args(self, param):
        if param is None or param == '':
            return None
        param = self.unpack(param)
        if self.min is not None and param <= self.min:
            param = self.min
        elif self.max is not None and param >= self.max:
            param = self.max
        format = self.format or '%s'
        extra_arg = format % param
        return self.opt + ' ' + extra_arg + ' '.join(self.extra or [])


class IntegerParam(BaseParam):
    _cast = int


class FloatParam(BaseParam):
    pass


class LogParam(FloatParam):

    def __init__(self, opt, **kwargs):
        FloatParam.__init__(self, opt, pack=np.log, unpack=np.exp, **kwargs)


class ValuesParam(BaseParam):

    def __init__(self, opt, values, **kwargs):
        BaseParam.__init__(self, opt, **kwargs)
        self.values = values

    def enumerate_all(self):
        return [self.get_extra_args(x) for x in self.values]


class BinaryParam(BaseParam):

    def __init__(self, opt, **kwargs):
        BaseParam.__init__(self, opt, **kwargs)

    def enumerate_all(self):
        return ['', self.opt]


PREPROCESSING_BINARY_OPTS = ['--%s' % x for x in 'htmlunescape lowercase strip_punct stem'.split()]


def get_format(value):
    """
    >>> get_format("1e-5")
    '%.0e'

    >>> get_format("1e5")
    '%.0e'

    >>> get_format("0.")
    '%.0g'

    >>> get_format("0.5")
    '%.1g'

    >>> get_format("0.5")
    '%.1g'

    >>> get_format("0.50")
    '%.2g'

    >>> get_format('5')
    """
    value = value.lower()

    if 'e' in value and '.' not in value:
        return '%.0e'

    x = value

    if '.' in x:
        x = x.split('.')[-1]

    if 'e' in x:
        x = x.split('e')[0]
        return '%%.%se' % len(x)

    if '.' in value:
        return '%%.%sg' % len(x)


DEFAULTS = {
    '--ngram': {
        'min': 1
    },
    '--l1': {
        'min': 1e-11
    },
    '--learning_rate': {
        'min': 0.000001
    }
}


def get_tuning_config(config):
    """
    >>> get_tuning_config('--lowercase?')
    BinaryParam(opt='--lowercase')

    >>> get_tuning_config('--ngram 2?')
    IntegerParam(opt='--ngram', init=2, min=1)

    >>> get_tuning_config('--ngram 2..?')
    IntegerParam(opt='--ngram', init=2, min=2)

    >>> get_tuning_config('--ngram 2..5?')
    IntegerParam(opt='--ngram', init=3, min=2, max=5)

    >>> get_tuning_config('-b 10..25?')
    IntegerParam(opt='-b', init=17, min=10, max=25)

    >>> get_tuning_config('--learning_rate 0.5?')
    FloatParam(opt='--learning_rate', init=0.5, min=1e-06, format='%.1g')

    >>> get_tuning_config('--learning_rate 0.50?')
    FloatParam(opt='--learning_rate', init=0.5, min=1e-06, format='%.2g')

    >>> get_tuning_config('--l1 1e-07?')
    LogParam(opt='--l1', init=1e-07, min=1e-11, format='%.0e')

    >>> get_tuning_config('--l1 1.0E-07?')
    LogParam(opt='--l1', init=1e-07, min=1e-11, format='%.1e')

    >>> get_tuning_config('--l1 ..1.2e-07..?')
    LogParam(opt='--l1', init=1.2e-07, format='%.1e')

    >>> get_tuning_config('--l1 1e-10..1e-05?')
    LogParam(opt='--l1', init=3e-08, min=1e-10, max=1e-05, format='%.0e')

    >>> get_tuning_config('--loss_function squared/hinge/percentile?')
    ValuesParam(opt='--loss_function', values=['squared', 'hinge', 'percentile'])

    >>> get_tuning_config('--loss_function /hinge/percentile?')
    ValuesParam(opt='--loss_function', values=['', 'hinge', 'percentile'])
    """
    if isinstance(config, basestring):
        config = config.split()

    if len(config) > 2:
        raise ValueError('Cannot parse: %r' % (config, ))

    first = config[0]

    assert first.startswith('-'), config

    if first.startswith('--'):
        prefix = '--'
        first = first[2:]
    else:
        prefix = '-'
        first = first[1:]

    if len(config) == 1:
        first = first[:-1]
        if '/' in first:
            return ValuesParam(opt='', values=[(prefix + x if x else '') for x in first.split('/')])
        else:
            return BinaryParam(prefix + first)

    value = config[-1]
    value = value[:-1]

    if '/' in value:
        return ValuesParam(opt=config[0], values=value.split('/'))

    is_log = 'e' in value.lower()

    if value.count('..') == 2:
        min, init, max = value.split('..')
        format = sorted([get_format(min), get_format(init), get_format(max)])[-1]
        is_float = '.' in min or '.' in init or '.' in max

        params = {
            'opt': config[0],
            'min': min,
            'init': init,
            'max': max,
            'format': format
        }

    elif '..' in value:
        min, max = value.split('..')
        is_float = '.' in min or '.' in max
        format = sorted([get_format(min), get_format(max)])[-1]

        params = {
            'opt': config[0],
            'min': min,
            'max': max,
            'format': format
        }

    else:
        is_float = '.' in value
        format = get_format(value)

        params = {
            'opt': config[0],
            'init': value,
            'format': format
        }

    for key, value in DEFAULTS.get(config[0], {}).items():
        if key not in params:
            params[key] = value

    if is_log:
        type = LogParam
    elif is_float:
        type = FloatParam
    else:
        type = IntegerParam

    return type(**params)


def vw_optimize_over_cv(vw_filename, folds, args, metric, is_binary, weight_metric, threshold,
                        predictions_filename=None, raw_predictions_filename=None, workers=None, other_metrics=[]):
    # we only depend on scipy if parameter tuning is enabled
    import scipy.optimize

    gridsearch_params = []
    tunable_params = []
    base_args = []
    assert isinstance(args, list), args

    for param in args:
        if isinstance(param, ValuesParam):
            gridsearch_params.append(param)
        elif isinstance(param, BaseParam):
            tunable_params.append(param)
        else:
            base_args.append(param)

    if predictions_filename:
        predictions_filename_tmp = predictions_filename + '.tuning'
    else:
        predictions_filename_tmp = get_temp_filename('predictions')
    if raw_predictions_filename:
        raw_predictions_filename_tmp = raw_predictions_filename + '.tuning'
    else:
        raw_predictions_filename_tmp = None

    extra_args = ['']
    cache = {}
    best_result = [None, None]

    y_true = _load_first_float_from_each_string(vw_filename)
    y_true = np.array(y_true)
    sample_weight = get_sample_weight(y_true, weight_metric)

    def run(params):
        log('Parameters: %r', params)
        args = extra_args[:]

        for param_config, param in zip(tunable_params, params):
            extra_arg = param_config.get_extra_args(param)
            if extra_arg:
                args.append(extra_arg)

        args = ' '.join(str(x) for x in args)
        args = re.sub('\s+', ' ', args).strip()

        if args in cache:
            return cache[args]

        log('Trying vw %s...', args)

        try:
            # XXX use return value
            vw_cross_validation(
                folds,
                args,
                workers=workers,
                p_fname=predictions_filename_tmp,
                r_fname=raw_predictions_filename_tmp)
        except BaseException, ex:
            log(str(ex))
            log('Result vw %s... error', args, log_level=1)
            cache[args] = 0.0
            return 0.0

        y_pred = _load_first_float_from_each_string(predictions_filename_tmp)
        y_pred = np.array(y_pred)
        assert len(y_true) == len(y_pred), (vw_filename, len(y_true), predictions_filename_tmp, len(y_pred), os.getpid())
        result = calculate_score(metric, y_true, y_pred, sample_weight, is_binary, threshold)

        if metric.replace('_w', '') in 'acc auc f1 precision recall'.split():
            result = -result
        else:
            assert metric.replace('_w', '') in ['mse', ''], metric

        is_best = ''
        if best_result[0] is None or result < best_result[0]:
            is_best = '*' if best_result[0] is not None else ''
            best_result[0] = result
            best_result[1] = args
            if predictions_filename:
                os.rename(predictions_filename_tmp, predictions_filename)
            if raw_predictions_filename:
                os.rename(raw_predictions_filename_tmp, raw_predictions_filename)

        unlink(predictions_filename_tmp, raw_predictions_filename_tmp)

        def frmt(x):
            if isinstance(x, float):
                return '%.4f' % x
            return str(x)

        other_results = ' '.join(['%s=%s' % (m, frmt(calculate_score(m, y_true, y_pred, sample_weight, is_binary, threshold))) for m in other_metrics])
        if other_results:
            other_results = '  ' + other_results

        log('Result vw %s... %s=%.4f%s%s', args, metric, abs(result), is_best, other_results, log_level=1 + bool(is_best))

        cache[args] = result
        return result

    already_done = {}

    log('Grid-search: %r', gridsearch_params)

    for params in expand(gridsearch_params):
        params_normalized = vw_normalize_params(base_args + params)
        if params_normalized != params:
            log('Normalized params %r %r -> %r', base_args, params, params_normalized, log_level=-1)
        params_as_str = ' '.join(params_normalized)
        if params_as_str in already_done:
            log('Skipping %r (same as %r)', ' '.join(params), ' '.join(already_done[params_as_str]), log_level=-1)
            continue
        already_done[params_as_str] = params

        extra_args[0] = params_as_str
        try:
            run([None] * len(tunable_params))
        except Exception:
            traceback.print_exc()
            continue

        scipy.optimize.minimize(run, [x.packed_init() for x in tunable_params], method='Nelder-Mead', options={'xtol': 0.001, 'ftol': 0.001})

    return best_result


def vw_normalize_params(params):
    """
    >>> vw_normalize_params(['--ngram', '1'])
    []
    >>> vw_normalize_params(['--ngram', '1', '--skips', '1'])
    []
    >>> vw_normalize_params(['--skips', '1'])
    []
    >>> vw_normalize_params(['--ngram', '2', '--skips', '1'])
    ['--ngram', '2', '--skips', '1']
    """
    params = ' '.join(params)
    params = params.replace('--ngram 1', '')
    if '--ngram' not in params:
        params = re.sub('--skips \d+', '', params)
    params = re.sub('\s+', ' ', params)
    return params.split()


def expand(gridsearch_params, only=None):
    for item in _expand(gridsearch_params, only=only):
        yield [x for x in item if x]


def _expand(gridsearch_params, only=None):
    if not gridsearch_params:
        yield []
        return

    first_arg = gridsearch_params[0]

    if isinstance(first_arg, basestring):
        skip = True
    elif only is not None and getattr(first_arg, 'opt', '') not in only:
        skip = True
    else:
        skip = False

    if skip:
        for inner in _expand(gridsearch_params[1:], only=only):
            yield [first_arg] + inner
        return

    for first_arg_variant in first_arg.enumerate_all():
        for inner in _expand(gridsearch_params[1:], only=only):
            yield [first_arg_variant] + inner


def get_language(doc):
    import pycld2
    if isinstance(doc, unicode):
        doc = doc.encode('utf-8')
    try:
        return pycld2.detect(doc, bestEffort=True)[2][0][0].lower()
    except Exception, ex:
        sys.stderr.write('Cannot detect language of %r\n%s\n' % (doc, ex))


def get_stemmer(language, stemmers={}):
    if language in stemmers:
        return stemmers[language]
    from nltk.stem import SnowballStemmer
    try:
        stemmers[language] = SnowballStemmer(language)
    except Exception:
        stemmers[language] = 0

    return stemmers[language]


def stem_words(words):
    base_stemmer = False
    result = []
    for word in words:
        if len(word) > 2:
            language = None
            try:
                language = get_language(word)
                stemmer = get_stemmer(language)
                if stemmer:
                    word = stemmer.stem(word)
                else:
                    if base_stemmer is False:
                        base_language = get_language(' '.join(words))
                        base_stemmer = get_stemmer(base_language)
                    if base_stemmer:
                        language = base_language
                        word = base_stemmer.stem(word)
            except Exception, ex:
                sys.stderr.write('Cannot stem %r %r: %s\n' % (language, word, ex))
        result.append(word)
    return result


class Preprocessor(object):
    ALL_OPTIONS = 'htmlunescape lowercase strip_punct stem'.split()

    @classmethod
    def parse_options(cls, string):
        parser = PassThroughOptionParser()
        for opt in PREPROCESSING_BINARY_OPTS:
            parser.add_option(opt, action='store_true')
        options, args = parser.parse_args(string.split())
        return options.__dict__

    @classmethod
    def from_options(cls, options):
        if isinstance(options, list):
            options = ' '.join(x for x in options if isinstance(x, basestring))

        if isinstance(options, basestring):
            options = cls.parse_options(options)

        for opt in cls.ALL_OPTIONS:
            if options[opt]:
                break
        else:
            return None

        return cls(**options)

    def to_options(self):
        return ['--%s' % opt for opt in self.ALL_OPTIONS if getattr(self, opt, None)]

    def __init__(self, htmlunescape=False, lowercase=False, strip_punct=False, stem=False, replace_currency=False, replace_numbers=False, normalize_space=True, **ignored):
        self.normalize_space = normalize_space
        self.htmlunescape = htmlunescape
        self.lowercase = lowercase
        self.strip_punct = strip_punct
        self.stem = stem
        if self.stem:
            stem_words(["testing"])
            self.lowercase = True
            self.strip_punct = True

    def __repr__(self):
        return '%s(%s)' % (type(self).__name__, ', '.join('%s=%r' % (name, getattr(self, name, None)) for name in self.ALL_OPTIONS))

    def process_text(self, text):
        orig = text
        try:
            text = text.decode('utf-8', errors='ignore')

            # quite costly
            # if self.normalize_space:
            #     text = u''.join(u' ' if unicodedata.category(x)[:1] in 'CZ' else x for x in text)

            if self.htmlunescape:
                text = htmlparser_unescape(text)

            if self.lowercase:
                text = text.lower()

            if self.strip_punct:
                words = re.findall(r"(?u)\b\w\w+\b", text)
            else:
                words = text.split()

            if self.stem:
                words = stem_words(words)

            text = u' '.join(words)
            return text.encode('utf-8')
        except Exception:
            sys.stderr.write('Failed to process\norig=%r\ntext=%r\n' % (orig, text))
            traceback.print_exc()
            raise

    def process_row(self, row):
        assert isinstance(row, list), row
        return [self.process_text(item) for item in row]

    def process_rows(self, rows):
        return [self.process_row(row) for row in rows]


def read_labels(filename, source, format, n_classes, columnspec, ignoreheader):
    labels_counts = {}
    examples_count = 0

    label_index = columnspec.index('y')

    log('Reading from %s', filename)
    rows_source = open_anything(filename or source, format)

    all_integers = True
    all_floats = True
    y_true = []

    for row in rows_source:
        if ignoreheader:
            ignoreheader = None
            continue
        label = row[label_index]
        y_true.append(label)
        n = labels_counts[label] = labels_counts.get(label, 0) + 1
        examples_count += 1
        if n == 1:
            if all_integers is True:
                try:
                    int(label)
                except Exception:
                    all_integers = False
            if all_floats is True:
                try:
                    float(label)
                except Exception:
                    all_floats = False

    if hasattr(source, 'seek'):
        source.seek(0)

    log('Counted %r examples', examples_count)

    if not labels_counts:
        sys.exit('empty: %s' % filename)

    labels = labels_counts.keys()

    def format_classes():
        classes = [(-count, name) for (name, count) in labels_counts.items()]
        classes.sort()
        classes = ['%s: %.2f%%' % (name, -100.0 * count / examples_count) for (count, name) in classes]
        if len(classes) > 6:
            del classes[3:-3]
            classes[3:3] = ['...']
        return ', '.join(classes)

    if all_integers:
        labels = [int(x) for x in labels]
        labels.sort()
        max_label = labels[-1]
        if min(labels) != 1:
            log('Minimum label is not 1, but %r', min(labels))
        if max_label != len(labels):
            log('Maximum label is %r, but total number of labels is %r', max_label, len(labels))
        if n_classes:
            if n_classes != len(labels):
                log('Expected %r classes, but found %r', n_classes, len(labels))
        if n_classes == 0:
            n_classes = max_label
        log('Found %r integer classes: %s', len(labels), format_classes(), log_level=1)

        # no mapping in this case
        labels = None
        y_true = np.array([int(x) for x in y_true])

    elif all_floats:
        labels = [float(x) for x in labels]
        log('Found float responses: %s..%s', min(labels), max(labels), log_level=1)
        if n_classes is not None:
            sys.exit('Float responses, not compatible with multiclass')
        labels = None
        y_true = np.array([float(x) for x in y_true])

    else:
        log('Found %r textual labels: %s', len(labels), format_classes(), log_level=1)

        if n_classes is None and len(labels) != 2:
            sys.exit('Found textual labels, expecting multiclass option. Pass 0 to auto-set number of classes to %r, e.g. "--oaa 0"' % len(labels))

        n_classes = len(labels)

        labels.sort()

    return labels_counts, y_true, labels, n_classes


def _make_proper_list(s, type=None):
    if not s:
        return s

    if isinstance(s, basestring):
        result = s.split(',')
        if type is not None:
            result = [type(x) for x in result]
        return result

    result = []
    if isinstance(s, list):
        for x in s:
            result.extend(_make_proper_list(x, type))
    else:
        raise TypeError('Expected list of string: %r' % (s, ))
    return result


def parse_weight(config, labels=None):
    """
    >>> parse_weight('A:B:2', ['A:B', 'another_label'])
    {'A:B': 2.0}

    >>> parse_weight('A:B:2')
    {'A:B': 2.0}
    """
    if not config:
        return None

    if config == ['balanced']:
        return config

    if labels and not isinstance(labels, list):
        raise TypeError('must be list, not %r' % type(labels))

    config = _make_proper_list(config)

    if not config:
        return None

    result = {}

    for item in config:
        if ':' not in item:
            sys.exit('Weight must be specified as CLASS:WEIGHT, cannot parse %r' % item)
        label, weight = item.rsplit(':', 1)

        if labels is not None and label not in labels:
            sys.exit('Label %r is not recognized. Expected: %r' % (label, labels))

        try:
            weight = float(weight)
        except Exception:
            weight = None

        if weight is None or weight < 0:
            sys.exit('Weight must be specified as CLASS:WEIGHT(float), %r is not recognized' % (item, ))

        if label in result:
            sys.exit('Label %r specified more than once' % label)
        result[label] = weight

    return result


def get_sample_weight(y_true, config):
    if config is None:
        return None
    N = len(y_true)
    result = np.zeros(N)
    updated = np.zeros(N)

    for klass, weight in config.items():
        assert isinstance(klass, (int, long, float)), [klass]
        result += np.multiply(np.ones(N) * weight, y_true == klass)
        updated += y_true == klass

    result += (updated == 0)

    return result


def get_balanced_weights(labels_counts):
    min_count = float(min(labels_counts.values()))

    result = {}
    for label in labels_counts:
        result[label] = min_count / labels_counts[label]

    log('Calculated balanced weights: %s', ' '.join('%s: %g' % (k, w) for (k, w) in sorted(result.items())), log_level=1)

    return result


def _convert_any_to_vw(source, format, output, labels, weights, preprocessor, columnspec, ignoreheader):
    assert format != 'vw'
    assert isinstance(columnspec, list)

    if labels:
        labels = dict((label, 1 + labels.index(label)) for label in labels)

    rows_source = open_anything(source, format)
    output = open(output, 'wb')

    errors = 0
    for row in rows_source:
        if ignoreheader:
            ignoreheader = None
            continue
        if len(row) != len(columnspec):
            sys.exit('Expected %r columns (%r), got %r (%r)' % (len(columnspec), columnspec, len(row), row))
        y = None
        x = []
        info = []
        for item, spec in zip(row, columnspec):
            if spec == 'y':
                y = item
            elif spec == 'text':
                x.append(item)
            elif spec == 'info':
                info.append(item)
            elif spec == 'drop' or not spec:
                continue
            else:
                sys.exit('Spec item %r not understood' % spec)

        assert y is not None, 'missing y'

        if info:
            info = " '%s" % ';'.join(info) + ' '
        else:
            info = ''

        if labels:
            vw_y = labels.get(y)
            if vw_y is None:
                log('Unexpected label: %s', limited_repr(y), log_level=2)
                errors += 1
                if errors > 5:
                    sys.exit(1)
                continue
        else:
            vw_y = y

        if weights is not None:
            weight = weights.get(y, 1)
            if weight == 1:
                weight = ''
            else:
                weight = str(weight) + ' '
        else:
            weight = ''

        if preprocessor:
            x = preprocessor.process_row(x)
            text = '  '.join(x)
        else:
            text = ' '.join(x)
        text = text.replace(':', ' ').replace('|', ' ')
        text = text.strip()
        text = '%s %s%s| %s\n' % (vw_y, weight, info, text)
        output.write(text)
        errors = 0

    output.flush()
    os.fsync(output.fileno())
    output.close()


def convert_any_to_vw(source, format, output_filename, labels, weights, columnspec, ignoreheader, preprocessor=None, shuffle=False, limit=None, workers=None):
    assert format != 'vw'

    start = time.time()

    preprocessor_opts = []
    if preprocessor:
        if hasattr(preprocessor, 'to_options'):
            preprocessor_opts.extend(preprocessor.to_options())
        elif isinstance(preprocessor, basestring):
            assert '--' in preprocessor, preprocessor
            preprocessor_opts.extend(preprocessor.split())
        else:
            preprocessor_opts.extend(preprocessor)

    workers = _workers(workers)
    batches, total_lines = split_file(source, nfolds=workers, shuffle=shuffle, limit=limit)
    batches_out = [x + '.out' for x in batches]

    labels = ','.join(labels or [])

    try:
        commands = []

        common_cmd = [quote(sys.executable), quote(__file__), '--format', format]

        if labels:
            common_cmd += ['--labels', quote(labels)]

        if weights:
            weights = ['%s:%s' % (x, weights[x]) for x in weights if weights[x] != 1]
            weights = ','.join(weights)
            common_cmd += ['--weight', quote(weights)]

        if columnspec:
            common_cmd += ['--columnspec', quote(','.join(str(x) for x in columnspec))]

        if ignoreheader:
            common_cmd += ['--ignoreheader']

        common_cmd.extend(preprocessor_opts)

        for batch in batches:
            cmd = common_cmd + ['--tovw_simple', batch + '.out', '-d', batch]
            commands.append(' '.join(cmd))

        if not run_subprocesses(commands, workers=workers, log_level=-1):
            sys.exit(1)

        cmd = 'cat ' + ' '.join(batches_out)
        if output_filename:
            cmd += ' > %s' % output_filename

        system(cmd, log_level=-1)

    finally:
        unlink(*batches)
        unlink(*batches_out)

    took = time.time() - start
    log('Generated %s in %.1f seconds', output_filename, took)
    if not output_filename.startswith('/dev/'):
        log('\n'.join(open(output_filename).read(200).split('\n')) + '...')


metrics_on_score = {
    'mse': 'mean_squared_error',
    'auc': 'roc_auc_score',
}

metrics_on_label = {
    'acc': 'accuracy_score',
    'precision': 'precision_score',
    'recall': 'recall_score',
    'f1': 'f1_score',
    'cm': 'confusion_matrix',
}


def calculate_score(metric, y_true, y_pred, sample_weight, is_binary, threshold, thresholds_used=set()):
    extra_args = {'sample_weight': sample_weight}
    if metric.endswith('_w'):
        metric = metric[:-2]
    else:
        extra_args = {}

    import sklearn.metrics
    if metric in metrics_on_score:
        fullname = metrics_on_score[metric]
        assert is_binary, 'Cannot apply %s/%s on multiclass' % (metric, fullname)
        func = getattr(sklearn.metrics, fullname)
        return func(y_true, y_pred, sample_weight=sample_weight)
    elif metric in metrics_on_label:
        fullname = metrics_on_label[metric]
        func = getattr(sklearn.metrics, fullname)
        if is_binary:
            if threshold is None:
                threshold = (min(y_true) + max(y_true)) / 2.0
            if threshold not in thresholds_used:
                thresholds_used.add(threshold)
                log('Using threshold: %g', threshold, log_level=1)
            y_true_norm = y_true > threshold
            y_pred_norm = y_pred > threshold
            return func(y_true_norm, y_pred_norm, **extra_args)
        else:
            return func(y_true, y_pred, **extra_args)
    else:
        sys.exit('Cannot calculate metric: %r' % metric)


def report(prefix, source, predictions, metric, n_classes, weight_metric, threshold):
    y_true = np.array(_load_first_float_from_each_string(source))
    y_pred = np.array(_load_first_float_from_each_string(predictions))
    sample_weight = get_sample_weight(y_true, weight_metric)

    for metric in metric:
        print '%s%s: %g' % (prefix, metric, calculate_score(metric, y_true, y_pred, sample_weight, is_binary=n_classes is None, threshold=threshold))


def preprocess_and_split(source, format, vw_filename, preprocessor, options, labels, weight_train, columnspec, ignoreheader):

    if format == 'vw':
        vw_filename = source
        # XXXX --shuffle and --limit make sense for vw format and should be supported
        # XXX preprocessor can make sense too
        assert not options.shuffle, 'TODO'
        assert not options.limit, 'TODO'
        assert not preprocessor, 'TODO'

    else:
        convert_any_to_vw(
            source,
            format,
            vw_filename,
            labels,
            weight_train,
            columnspec,
            ignoreheader,
            preprocessor=preprocessor,
            shuffle=options.shuffle,   # XXX must also set seed
            limit=options.limit,
            workers=options.workers)

    folds, total = split_file(vw_filename, nfolds=options.nfolds, limit=options.limit)
    for fname in folds:
        assert os.path.exists(fname), fname

    return folds


def main_tune(options, source, format, vw_filename, args, preprocessor_base, labels, weight_metric, weight_train, columnspec, ignoreheader, is_binary):
    if preprocessor_base is None:
        preprocessor_base = []
    else:
        preprocessor_base = preprocessor_base.to_options()

    if not options.metric:
        sys.exit('Provide metric to optimize for with --metric auc|acc|mse|f1')

    optimization_metric = options.metric[0]
    other_metrics = options.metric[1:]
    assert not options.audit, '-a incompatible with parameter tuning'

    best_preprocessor_opts = None
    best_vw_options = None
    best_result = None
    already_done = {}

    preprocessor_variants = list(expand(args, only=PREPROCESSING_BINARY_OPTS))
    log('Trying preprocessor variants: %r', preprocessor_variants)

    for my_args in preprocessor_variants:
        preprocessor = Preprocessor.from_options(preprocessor_base + my_args)
        preprocessor_opts = ' '.join(preprocessor.to_options() if preprocessor else [])
        if preprocessor_opts:
            log('Trying preprocessor: %s', preprocessor_opts)

        previously_done = already_done.get(str(preprocessor))

        if previously_done:
            log('Same as %s', previously_done)
            continue

        already_done[str(preprocessor)] = preprocessor_opts

        folds = preprocess_and_split(source, format, vw_filename, preprocessor, options, labels, weight_train, columnspec, ignoreheader)

        vw_args = [x for x in my_args if x not in PREPROCESSING_BINARY_OPTS]

        try:
            this_best_result, this_best_options = vw_optimize_over_cv(
                vw_filename,
                folds,
                vw_args,
                optimization_metric,
                is_binary,
                weight_metric,
                options.threshold,
                predictions_filename=options.cv_predictions,
                raw_predictions_filename=options.cv_raw_predictions,
                workers=options.workers,
                other_metrics=other_metrics)
        finally:
            unlink(*folds)

        is_best = ''
        if this_best_result is not None and (best_result is None or this_best_result < best_result):
            best_result = this_best_result
            best_vw_options = this_best_options
            best_preprocessor_opts = preprocessor_opts
            is_best = '*'

        if preprocessor_opts:
            print 'Best options with %s: %s' % (preprocessor_opts or 'no preprocessing', this_best_options, )
        print 'Best %s with %r: %.4f%s' % (optimization_metric, preprocessor_opts or 'no preprocessing', abs(this_best_result or 0.0), is_best)
        # print 'Improvement over no l1=%.4f. Improvement over initial guess=%.4f' % (no_l1_result - best_result[0], initial_l1_result - best_result[0])

    # XXX don't show this if preprocessor is not enabled and not tuned
    print 'Best preprocessor options: %s' % (best_preprocessor_opts or '<none>', )
    print 'Best vw options: %s' % (best_vw_options, )
    print 'Best %s: %.4f' % (optimization_metric, abs(best_result))
    # print 'Improvement over no l1=%.4f. Improvement over initial guess=%.4f' % (no_l1_result - best_result[0], initial_l1_result - best_result[0])
    preprocessor = Preprocessor.from_options(best_preprocessor_opts)
    return best_vw_options, preprocessor


def main():
    parser = PassThroughOptionParser()

    # cross-validation and parameter tuning options
    parser.add_option('--cv', action='store_true')
    parser.add_option('--cv_predictions')
    parser.add_option('--cv_raw_predictions')
    parser.add_option('--cv_audit')
    parser.add_option('--cv_errors', action='store_true')
    parser.add_option('--workers', type=int)
    parser.add_option('--nfolds', type=int)
    parser.add_option('--metric', action='append')
    parser.add_option('--limit', type=int, help="Only read first N lines from the source file")

    # class weight option
    parser.add_option('--weight', action='append', help='Class weights to use in CLASS:WEIGHT format', default=[])
    parser.add_option('--weight_train', action='append', help='Class weight to use (during training only), in CLASS:WEIGHT format', default=[])
    parser.add_option('--weight_metric', action='append', help='Class weight to use (for weighted metrics only), in CLASS:WEIGHT format', default=[])

    # vowpal wabbit arguments (those that we care about. everything else is passed through)
    parser.add_option('-r', '--raw_predictions')
    parser.add_option('-p', '--predictions')
    parser.add_option('-f', '--final_regressor')
    parser.add_option('-d', '--data')
    parser.add_option('-a', '--audit', action='store_true')

    # preprocessing options:
    parser.add_option('--shuffle', action='store_true')
    parser.add_option('--labels')
    for opt in Preprocessor.ALL_OPTIONS:
        parser.add_option('--%s' % opt, action='store_true')
    parser.add_option('--columnspec', default='y,text')
    parser.add_option('--ignoreheader', action='store_true')

    # using preprocessor standalone:
    parser.add_option('--tovw')
    parser.add_option('--tovw_simple')

    # using as perf
    parser.add_option('--report', action='store_true')
    parser.add_option('--toperrors', action='store_true')
    parser.add_option('--threshold', default=0.0, type=float)

    # logging and debugging and misc
    parser.add_option('--morelogs', action='count', default=0)
    parser.add_option('--lesslogs', action='count', default=0)
    parser.add_option('--format', help='File format, one of vw|tsv|csv. If not provided, will be guessed from file extension or from file contents')

    options, args = parser.parse_args()

    globals()['LOG_LEVEL'] += options.lesslogs - options.morelogs

    options.weight = parse_weight(options.weight, options.labels)
    options.weight_train = parse_weight(options.weight_train, options.labels) or options.weight
    options.weight_metric = parse_weight(options.weight_metric, options.labels) or options.weight
    options.labels = _make_proper_list(options.labels)

    if options.labels and len(options.labels) <= 1:
        sys.exit('Expected comma-separated list of labels: --labels %r\n' % options.labels)

    used_stdin = False
    if options.data in (None, '/dev/stdin', '-'):
        used_stdin = True
        from StringIO import StringIO
        input = sys.stdin.read()
        source = StringIO(input)
        filename = None
    else:
        source = None
        filename = options.data

    options.columnspec = _make_proper_list(options.columnspec)

    if options.tovw_simple:
        assert not options.shuffle
        assert not options.limit
        assert not options.workers or options.workers == 1, options.workers
        assert options.format and options.format in ('vw', 'csv', 'tsv')
        assert options.weight_train != 'balanced', 'not supported here'
        preprocessor = Preprocessor.from_options(options.__dict__)
        _convert_any_to_vw(
            source or filename,
            options.format,
            options.tovw_simple,
            options.labels,
            options.weight_train,
            preprocessor,
            options.columnspec,
            options.ignoreheader)
        sys.exit(0)

    if not options.cv:
        for key, value in options.__dict__.items():
            if value and key.startswith('cv_'):
                options.cv = True
                break

    n_classes = None
    vw_multiclass_opts = 'oaa|ect|csoaa|log_multi|recall_tree'

    n_classes_cmdline = re.findall('--(?:%s)\s+(\d+)' % vw_multiclass_opts, ' '.join(args))
    if n_classes_cmdline:
        n_classes = max(int(x) for x in n_classes_cmdline)

    format = options.format

    if format and format not in ('vw', 'csv', 'tsv'):
        sys.exit('--format must one of vw,csv,tsv, not %r' % format)

    if not format:
        format = guess_format(filename or source)

    if options.labels:
        orig_labels = options.labels
        if len(options.labels) <= 1:
            sys.exit('Expected comma-separated list of labels: --labels %r\n' % orig_labels)
        if n_classes == 0:
            n_classes = len(options.labels)
    else:
        labels_counts, y_true, labels, n_classes = read_labels(filename, source, format, n_classes, options.columnspec, options.ignoreheader)
        options.labels = labels

    labels = options.labels

    if n_classes:
        args = re.sub('(--(?:%s)\s+)(0)' % vw_multiclass_opts, '\\g<1>' + str(n_classes), ' '.join(args)).split()

    balanced_weights = None

    if options.weight_train == ['balanced']:
        options.weight_train = balanced_weights = get_balanced_weights(labels_counts)

    if options.weight_metric == ['balanced']:
        options.weight_metric = balanced_weights or get_balanced_weights(labels_counts)

    if options.weight_metric:
        if labels:
            options.weight_metric = dict((labels.index(key) + 1, weight) for (key, weight) in options.weight_metric.items())
        else:
            options.weight_metric = dict((float(key), weight) for (key, weight) in options.weight_metric.items())

    if options.tovw:
        preprocessor = Preprocessor.from_options(options.__dict__)

        assert format != 'vw', 'Input should be csv or tsv'  # XXX
        convert_any_to_vw(
            source or filename,
            format,
            options.tovw,
            labels,
            options.weight_train,
            options.columnspec,
            options.ignoreheader,
            preprocessor,
            shuffle=options.shuffle,
            limit=options.shuffle,
            workers=options.workers)
        sys.exit(0)

    to_cleanup = []
    options.metric = _make_proper_list(options.metric)

    if options.report:
        if not options.metric:
            options.metric = ['mse']
        if options.predictions in ('/dev/stdin', '-'):
            if used_stdin:
                sys.exit('Can only use /dev/stdin in one argument')
            predictions = sys.stdin
        elif options.predictions:
            predictions = options.predictions
        else:
            sys.exit('Must provide -p')

        y_pred = np.array(_load_first_float_from_each_string(predictions))
        sample_weight = get_sample_weight(y_true, options.weight_metric)

        for metric in options.metric:
            print '%s: %g' % (metric, calculate_score(metric, y_true, y_pred, sample_weight, is_binary=n_classes is None, threshold=options.threshold))

        sys.exit(0)

    preprocessor = Preprocessor.from_options(options.__dict__)

    if format == 'vw':
        assert not preprocessor or not preprocessor.to_options(), preprocessor
        vw_filename = filename
    else:
        vw_filename = get_temp_filename('vw')
        to_cleanup.append(vw_filename)

    folds = []
    need_tuning = 0

    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith('-'):
            next_arg = args[index + 1] if index + 1 < len(args) else ''
            if arg.endswith('?'):
                need_tuning += 1
                args[index] = get_tuning_config(arg)
            elif next_arg.endswith('?'):
                need_tuning += 1
                args[index:index + 2] = [get_tuning_config(arg + ' ' + next_arg)]
        index += 1

    try:

        if need_tuning:
            final_options, preprocessor = main_tune(options, source or filename, format, vw_filename, args, preprocessor, labels,
                                                    options.weight_metric, options.weight_train, options.columnspec, options.ignoreheader, is_binary=n_classes is None)
            # XXX must leave vw_filename preprocessed with correct preprocessor!
            # XXX also need --cv_predictions / --cv_errors to work. simply do cv afterwards?

        else:
            final_options = ' '.join(args)

            if format != 'vw':
                convert_any_to_vw(
                    source or filename,
                    format,
                    vw_filename,
                    labels,
                    options.weight_train,
                    options.columnspec,
                    options.ignoreheader,
                    preprocessor=preprocessor,
                    shuffle=options.shuffle,   # XXX must also set seed
                    limit=options.limit,
                    workers=options.workers)

            if options.cv:
                # Note, do not put shuffle there, since we want vw_filename to correspond to --cv_predictions
                # Shuffling is done above
                folds, total_lines = split_file(vw_filename, nfolds=options.nfolds, limit=options.limit)

                assert len(folds) >= 2, folds

                cv_predictions = options.cv_predictions
                if not cv_predictions:
                    cv_predictions = get_temp_filename('cvpred')
                    to_cleanup.append(cv_predictions)

                cv_pred = vw_cross_validation(
                    folds,
                    final_options,
                    workers=options.workers,
                    p_fname=cv_predictions,
                    r_fname=options.cv_raw_predictions,
                    audit=options.cv_audit)

                if options.metric:
                    report('cv ', vw_filename, cv_pred, options.metric, n_classes, options.weight_metric, options.threshold)

    finally:
        unlink(*folds)

    if options.final_regressor or not (options.cv or need_tuning):
        vw_cmd = 'vw %s -d %s' % (final_options, vw_filename)
        if options.final_regressor:
            # vw sometimes does not build model but does not signal error with returncode either
            vw_cmd += ' -f %s' % options.final_regressor

        predictions_fname = options.predictions

        if options.metric:
            if not predictions_fname:
                predictions_fname = get_temp_filename('pred')

        if predictions_fname:
            vw_cmd += ' -p %s' % predictions_fname

        if options.raw_predictions:
            vw_cmd += ' -r %s' % options.raw_predictions

        if options.audit:
            vw_cmd += ' -a'

        system(vw_cmd, log_level=0)

        if options.metric:
            y_pred = np.array(_load_first_float_from_each_string(predictions_fname))
            sample_weight = get_sample_weight(y_true, options.weight_metric)

            for metric in options.metric:
                print '%s: %g' % (metric, calculate_score(metric, y_true, y_pred, sample_weight, is_binary=n_classes is None, threshold=options.threshold))

        if options.toperrors:
            errors = []

            if hasattr(source, 'seek'):
                source.seek(0)

            for yp, yt, example in zip(y_pred, y_true, open_anything(source or filename, format)):
                errors.append((abs(yp - yt), yp, example))

            errors.sort(reverse=True)
            output = csv.writer(sys.stdout)

            for err, yp, example in errors:
                row = [err]
                if isinstance(example, list):
                    row.extend(example)
                output.writerow(row)

        sys.exit(0)

    unlink(*to_cleanup)


if __name__ == '__main__':
    main()
