import argparse
import collections
import fastavro
import json
import logging
import numpy as np
import os
import tensorflow as tf
import time

from gdmix.data import BAYESIAN_LINEAR_MODEL_SCHEMA
from tensorflow.python import pywrap_tensorflow

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def try_write_avro_blocks(f, schema, records, suc_msg=None, err_msg=None):
    """
    write a block into avro file. This is used continuously when the whole file does not fit in memory.

    :param f: file handle.
    :param schema: avro schema used by the writer.
    :param records: a set of records to be written to the avro file.
    :param suc_msg: message to print when write succeeds.
    :param err_msg: message to print when write fails.
    :return: none
    """
    try:
        fastavro.writer(f, schema, records)
        records[:] = []
        if suc_msg:
            logger.info(suc_msg)
    except Exception as exp:
        if err_msg:
            logger.error(exp)
            logger.error(err_msg)
        raise


def load_scipy_models_from_avro(model_file):
    """ load model(s) from avro file. """

    def get_one_model_weights(model_record):
        """ load one model weights. """
        model_coefficients = []
        for ntv in model_record["means"]:
            model_coefficients.append(np.float64(ntv['value']))
        bias = model_coefficients[0]
        weights = model_coefficients[1:]
        return np.array(weights + [bias])

    models = []
    with tf.io.gfile.GFile(model_file, 'rb') as fo:
        avro_reader = fastavro.reader(fo)
        for record in avro_reader:
            model_coefficients = get_one_model_weights(record)
            models.append(model_coefficients)
    return models


def gen_one_avro_model(model_id, model_class, weight_indices, weight_values, bias, feature_list):
    """
    generate the record for one LR model in photon-ml avro format
    :param model_id: model id
    :param model_class: model class
    :param weight_indices: LR weight vector indices
    :param weight_values: LR weight vector values
    :param bias: the bias/offset/intercept
    :param feature_list: corresponding feature names
    :return: a model in avro format
    """
    records = {u'modelId': model_id, u'modelClass': model_class, u'means': [],
               u'lossFunction': ""}
    record = {u'name': '(INTERCEPT)', u'term': '', u'value': bias}
    records[u'means'].insert(0, record)
    for w_i, w_v in zip(weight_indices.flatten(), weight_values.flatten()):
        feat = feature_list[w_i]
        record = {u'name': feat[0], u'term': feat[1], u'value': w_v}
        records[u'means'].append(record)
    return records


def export_scipy_lr_model_to_avro(model_ids,
                                  list_of_weight_indices,
                                  list_of_weight_values,
                                  biases,
                                  feature_file,
                                  output_file,
                                  model_log_interval=1000,
                                  model_class="com.linkedin.photon.ml.supervised.classification.LogisticRegressionModel"
                                  ):
    """
    Export scipy-based random effect logistic regression model in avro format for photon-ml to consume
    :param model_ids:               a list of model ids used in generated avro file
    :param list_of_weight_indices:  list of indices for entity-specific model weights
    :param list_of_weight_values:   list of values for entity-specific model weights
    :param biases:                  list of entity bias terms
    :param feature_file:            a file containing all the features, typically generated by avro2tf.
    :param output_file:             full file path for the generated avro file.
    :param model_log_interval:      write model every model_log_interval models.
    :param model_class:             the model class defined by photon-ml.
    :return: None
    """
    # STEP [1] - Read feature list
    feature_list = read_feature_list(feature_file)

    # STEP [2] - Read number of features and moels
    num_features = len(feature_list)
    num_models = len(biases)
    logger.info("found {} models".format(num_models))
    logger.info("num features: {}".format(num_features))

    # STEP [3]
    model_avro_records = []
    schema = fastavro.parse_schema(json.loads(BAYESIAN_LINEAR_MODEL_SCHEMA))
    with tf.io.gfile.GFile(output_file, 'wb') as f:
        f.seekable = lambda: False
        records = gen_one_avro_model(str(model_ids[0]), model_class, list_of_weight_indices[0],
                                     list_of_weight_values[0],
                                     biases[0], feature_list)
        model_avro_records.append(records)
        err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[0], output_file)
        try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)

    # write the remaining models
    with tf.io.gfile.GFile(output_file, 'ab+') as f:
        f.seek(0, 2)  # seek to the end of the file, 0 is offset, 2 means the end of file
        f.seekable = lambda: True
        f.readable = lambda: True
        for i in range(1, num_models):
            records = gen_one_avro_model(str(model_ids[i]), model_class, list_of_weight_indices[i],
                                         list_of_weight_values[i],
                                         biases[i], feature_list)
            model_avro_records.append(records)
            if i % model_log_interval == 0:
                err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[i], output_file)
                try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)
        if len(model_avro_records):
            err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[-1], output_file)
            try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)
    logger.info("dumped avro model file at {}".format(output_file))


def export_lr_model_to_avro(checkpoint_dir, feature_file, output_file,
                            weight_variable_list=["global_weights"],
                            bias="bias",
                            model_ids=["global model"],
                            model_log_interval=1000,
                            model_class="com.linkedin.photon.ml.supervised.classification.LogisticRegressionModel"):
    """
    read from checkpoint dir and export the logistic regression model in avro format for photon-ml to consume.

    :param checkpoint_dir: directory where checkpoint files are saved by tensorflow.
    :param feature_file: a file containing all the features, typically generated by avro2tf.
    :param output_file: full file path for the generated avro file.
    :param weight_variable_list: a list of LR weights in the tensorflow graph.
    :param bias: the bias variable in the tensorflow graph.
    :param model_ids: a list of model ids used in generated avro file.
    :param model_log_interval: write model every model_log_interval models.
    :param model_class: the model class defined by photon-ml.
    :return: none
    """
    reader = pywrap_tensorflow.NewCheckpointReader(tf.train.latest_checkpoint(checkpoint_dir))
    assert (len(weight_variable_list) == 1)  # only one feature bag is supported.
    weights = reader.get_tensor(weight_variable_list[0])
    bias = reader.get_tensor(bias)
    logger.info("{} shape: {}".format(weight_variable_list[0], weights.shape))
    model_ids = [str(x) for x in model_ids]  # convert model ids to string.

    # read feature list
    feature_list = read_feature_list(feature_file)
    num_features = len(feature_list)
    num_models = weights.shape[0]
    weights_shape = weights.shape[1]
    logger.info("found {} models".format(num_models))
    logger.info("weight shape: {}".format(weights_shape))
    logger.info("num features: {}".format(num_features))
    assert (weights.shape[1] == num_features)
    assert (num_models == len(model_ids))
    weight_indices = np.arange(weights.shape[1])

    # write avro records
    model_avro_records = []
    schema = fastavro.parse_schema(json.loads(BAYESIAN_LINEAR_MODEL_SCHEMA))

    with tf.io.gfile.Gfile(output_file, 'wb') as f:
        f.seekable = lambda: False
        records = gen_one_avro_model(model_ids[0], model_class, weight_indices, weights[0, :], bias[0], feature_list)
        model_avro_records.append(records)
        err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[0], output_file)
        try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)

    # write the remaining models
    with tf.io.gfile.GFile(output_file, 'ab+') as f:
        f.seek(0, 2)  # seek to the end of the file, 0 is offset, 2 means the end of file
        f.seekable = lambda: True
        f.readable = lambda: True
        for i in range(1, num_models):
            records = gen_one_avro_model(model_ids[i], model_class, weight_indices, weights[i, :], bias[i],
                                         feature_list)
            model_avro_records.append(records)
            if i % model_log_interval == 0:
                err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[i], output_file)
                try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)
        if len(model_avro_records):
            err_msg = 'An error occurred while writing model id {} to path {}'.format(model_ids[-1], output_file)
            try_write_avro_blocks(f, schema, model_avro_records, None, err_msg)
    logger.info("dumped avro model file at {}".format(output_file))


def read_feature_list(feature_file):
    feature_list = []
    with tf.io.gfile.GFile(feature_file) as f:
        f.seekable = lambda: False
        for line in f:
            fields = line.strip().split(',')
            if len(fields) == 1:
                fields.append('')
            feature_list.append(fields)
    return feature_list


def read_json_file(file_path: str):
    """ Load a json file from a path.

    :param file_path: Path string to json file.
    :return: dict. The decoded json object.

    Raises IOError if path does not exist.
    Raises ValueError if load fails.
    """

    if not tf.io.gfile.exists(file_path):
        raise IOError("Path '{}' does not exist.".format(file_path))
    try:
        with tf.io.gfile.GFile(file_path) as json_file:
            return json.load(json_file)
    except Exception as e:
        raise ValueError("Error '{}' while loading file '{}'."
                         .format(e, file_path))


def str2bool(v):
    """
    handle argparse can't parse boolean well.
    ref: https://stackoverflow.com/questions/15008758/parsing-boolean-values-with-argparse/36031646
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() == 'true'
    else:
        raise argparse.ArgumentTypeError('Boolean or string value expected.')


def copy_files(input_files, output_dir):
    """
    Copy a list of files to the output directory.
    The destination files will be overwritten.
    :param input_files: a list of files
    :param output_dir: output directory
    :return: the list of copied files
    """

    logger.info("Copy files to local")
    if not tf.io.gfile.exists(output_dir):
        tf.io.gfile.mkdir(output_dir)
    start_time = time.time()
    copied_files = []
    for f in input_files:
        fname = os.path.join(output_dir, os.path.basename(f))
        tf.io.gfile.copy(f, fname, overwrite=True)
        copied_files.append(fname)
    logger.info("Files copied to Local: {}".format(copied_files))
    logger.info("--- %s seconds ---" % (time.time() - start_time))
    return copied_files


def namedtuple_with_defaults(typename, field_names, defaults=()):
    """
    Namedtuple with default values is supported since 3.7, wrap it to be compatible with version <= 3.6
    :param typename: the type name of the namedtuple
    :param field_names: the field names of the namedtuple
    :param defaults: the default values of the namedtuple
    :return: namedtuple with defaults
    """
    T = collections.namedtuple(typename, field_names)
    T.__new__.__defaults__ = (None,) * len(T._fields)
    if isinstance(defaults, collections.Mapping):
        prototype = T(**defaults)
    else:
        prototype = T(*defaults)
    T.__new__.__defaults__ = tuple(prototype)
    return T
