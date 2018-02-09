import unittest
import shutil
import tempfile
import os
import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
import coremltools
from os.path import dirname

from tensorflow.python.tools.freeze_graph import freeze_graph
import tfcoreml as tf_converter


"""IMPORTANT NOTE TO ADD NEW TESTS:
For each test function you should set up your own graph and session.
Otherwise TF will carry all ops and tensors from previously run tests.
"""

def _tf_transpose(x, is_sequence=False):
  if not hasattr(x, "shape"):
    return x
  if len(x.shape) == 4:
    # [Batch, Height, Width, Channels] --> [Batch, Channels, Height, Width]
    x = np.transpose(x, [0,3,1,2])
    return np.expand_dims(x, axis=0)
  elif len(x.shape) == 3:
    # We only deal with non-recurrent networks for now
    # [Batch, (Sequence) Length, Channels] --> [1,B, Channels, 1, Seq]
    # [0,1,2] [0,2,1]
    return np.transpose(x, [0,2,1])[None,:,:,None,:]
  elif len(x.shape) == 2:
    if is_sequence:  # (N,S) --> (S,N,1,)
      return x.reshape(x.shape[::-1] + (1,))
    else:  # (N,C) --> (N,C,1,1)
      return x.reshape((1, ) + x.shape) # Dense
  elif len(x.shape) == 1:
    if is_sequence: # (S) --> (S,N,1,1,1)
      return x.reshape((x.shape[0], 1, 1))
    else:
      return x
  else:
    return x

def _convert_to_coreml(tf_model_path, mlmodel_path, input_name_shape_dict,
    output_names):
  """ Convert and return the coreml model from the Tensorflow
  """
  model = tf_converter.convert(tf_model_path=tf_model_path,
                                mlmodel_path=mlmodel_path,
                                output_feature_names=output_names,
                                input_name_shape_dict=input_name_shape_dict)
  return model

def _generate_data(input_shape, mode = 'random'):
  """
  Generate some random data according to a shape.
  """
  if input_shape is None or len(input_shape) == 0:
    return 0.5
  if mode == 'zeros':
    X = np.zeros(input_shape)
  elif mode == 'ones':
    X = np.ones(input_shape)
  elif mode == 'linear':
    X = np.array(range(np.product(input_shape))).reshape(input_shape)*1.0
  elif mode == 'random':
    X = np.random.rand(*input_shape)
  elif mode == 'random_zero_mean':
    X = np.random.rand(*input_shape)-0.5
  return X

class TFNetworkTest(unittest.TestCase):

  @classmethod
  def setUpClass(self):
    """ Set up the unit test by loading common utilities.
    """

  def _simple_freeze(self, input_graph, input_checkpoint, output_graph,
      output_node_names):
    # output_node_names is a string of names separated by comma
    freeze_graph(input_graph=input_graph,
                 input_saver="",
                 input_binary=False,
                 input_checkpoint=input_checkpoint,
                 output_node_names=output_node_names,
                 restore_op_name="save/restore_all",
                 filename_tensor_name="save/Const:0",
                 output_graph=output_graph,
                 clear_devices=True,
                 initializer_nodes="")

  def _test_coreml_conversionl(self, model_dir, frozen_model_file, coreml_model_file,
            output_node_names, input_tensor_shapes, one_dim_seq_flags,
            feed_dict, tf_result, delta, use_cpu_only):
    # convert the tensorflow model
    output_tensor_names = [name + ':0' for name in output_node_names]
    coreml_model = _convert_to_coreml(
        tf_model_path=frozen_model_file,
        mlmodel_path=coreml_model_file,
        input_name_shape_dict=input_tensor_shapes,
        output_names=output_tensor_names)

    # evaluate coreml
    coreml_inputs = {}
    for idx, in_tensor_name in enumerate(input_tensor_shapes):
      in_shape = input_tensor_shapes[in_tensor_name]
      coreml_in_name = in_tensor_name.replace(':', '__').replace('/', '__')
      if one_dim_seq_flags is None:
        coreml_inputs[coreml_in_name] = _tf_transpose(
            feed_dict[in_tensor_name]).copy()
      else:
        coreml_inputs[coreml_in_name] = _tf_transpose(
            feed_dict[in_tensor_name], one_dim_seq_flags[idx]).copy()

    coreml_output = coreml_model.predict(coreml_inputs, useCPUOnly=use_cpu_only)

    for idx, out_name in enumerate(output_node_names):
      tp = _tf_transpose(tf_result[idx]).flatten()
      out_tensor_name = out_name.replace('/','__') + '__0'
      cp = coreml_output[out_tensor_name].flatten()
      self.assertEquals(len(tp), len(cp))
      for i in range(len(tp)):
        max_den = max(1.0, tp[i], cp[i])
        self.assertAlmostEquals(tp[i]/max_den, cp[i]/max_den, delta=delta)

    # Cleanup files - models on disk no longer useful
    if os.path.exists(model_dir):
      shutil.rmtree(model_dir)

  def _test_tf_model(self, graph, input_tensor_shapes, output_node_names,
      data_mode = 'random', delta = 1e-2, use_cpu_only = False,
      one_dim_seq_flags = None):
    """ Common entry to testing routine.
    graph - defined TensorFlow graph.
    input_tensor_shapes -  dict str:shape for each input (placeholder)
    output_node_names - output_node_names, a list of strings
    output_tensor_names - output tensor names, a list of strings, usually
        just output_node_names each appended with ':0'
    """

    # Some file processing
    model_dir = tempfile.mkdtemp()
    graph_def_file = os.path.join(model_dir, 'tf_graph.pbtxt')
    checkpoint_file = os.path.join(model_dir, 'tf_model.ckpt')
    frozen_model_file = os.path.join(model_dir, 'tf_frozen.pb')
    coreml_model_file = os.path.join(model_dir, 'coreml_model.mlmodel')

    # add a saver
    tf.reset_default_graph()
    with graph.as_default() as g:
      saver = tf.train.Saver()

    with tf.Session(graph = graph) as sess:
      # initialize
      sess.run(tf.global_variables_initializer())
      # prepare the tensorflow inputs
      feed_dict = {}
      for in_tensor_name in input_tensor_shapes:
        in_tensor_shape = input_tensor_shapes[in_tensor_name]
        feed_dict[in_tensor_name] = _generate_data(in_tensor_shape, data_mode)
      # run the result
      fetches = [graph.get_operation_by_name(name).outputs[0] for name in \
          output_node_names]
      tf_result = sess.run(fetches, feed_dict=feed_dict)
      # save graph definition somewhere
      tf.train.write_graph(sess.graph, model_dir, graph_def_file)
      # save the weights
      saver.save(sess, checkpoint_file)

    # freeze the graph
    self._simple_freeze(
        input_graph=graph_def_file,
        input_checkpoint=checkpoint_file,
        output_graph=frozen_model_file,
        output_node_names=",".join(output_node_names))

    #convert and test numerical accuracy with CoreML
    self._test_coreml_conversionl(model_dir, frozen_model_file, coreml_model_file,
            output_node_names, input_tensor_shapes, one_dim_seq_flags,
            feed_dict, tf_result, delta, use_cpu_only)

  def _test_tf_model_constant(self, graph, input_tensor_shapes, output_node_names,
      data_mode='random', delta=1e-2, use_cpu_only=False,
      one_dim_seq_flags=None):

    """ Common entry to testing routine for graphs that have no variables.
      graph - defined TensorFlow graph.
      input_tensor_shapes -  dict str:shape for each input (placeholder)
      output_node_names - output_node_names, a list of strings
      output_tensor_names - output tensor names, a list of strings, usually
          just output_node_names each appended with ':0'
    """

    model_dir = tempfile.mkdtemp()
    frozen_model_file = os.path.join(model_dir, 'tf_frozen.pb')
    coreml_model_file = os.path.join(model_dir, 'coreml_model.mlmodel')

    with tf.Session(graph = graph) as sess:
      # initialize
      sess.run(tf.global_variables_initializer())
      # prepare the tensorflow inputs
      feed_dict = {}
      for in_tensor_name in input_tensor_shapes:
        in_tensor_shape = input_tensor_shapes[in_tensor_name]
        feed_dict[in_tensor_name] = _generate_data(in_tensor_shape, data_mode)

      # run the result
      fetches = [graph.get_operation_by_name(name).outputs[0] for name in \
          output_node_names]
      tf_result = sess.run(fetches, feed_dict=feed_dict)

      #save the frozen .pb
      output_graph_def = tf.graph_util.convert_variables_to_constants(
        sess, # The session is used to retrieve the weights
        tf.get_default_graph().as_graph_def(), # The graph_def is used to retrieve the nodes
        output_node_names #The output node names are used to select the usefull nodes
        )
      with tf.gfile.GFile(frozen_model_file, "wb") as f:
          f.write(output_graph_def.SerializeToString())

    #convert and test numerical accuracy with CoreML
    self._test_coreml_conversionl(model_dir, frozen_model_file, coreml_model_file,
            output_node_names, input_tensor_shapes, one_dim_seq_flags,
            feed_dict, tf_result, delta, use_cpu_only)



class TFSimpleNetworkTest(TFNetworkTest):

  def test_toy(self):
    # Define your TF graph here
    graph = tf.Graph()
    with graph.as_default() as g:
      # matrix1 is input of shape (Batch=1,Channels=2)
      matrix1 = tf.placeholder(tf.float32, shape=[1,2], name="test_toy/input")
      matrix2 = tf.Variable(tf.truncated_normal([2,1]))
      product = tf.matmul(matrix1, matrix2, name = "test_toy/product")
      saver = tf.train.Saver()

    self._test_tf_model(graph, {"test_toy/input:0":[1,2]},
        ["test_toy/product"], delta=1e-2)

  def test_linear(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      # placeholder constructor returns a tensor not an op
      x = tf.placeholder(tf.float32, shape=[None,20], name="test_linear/input")
      # Make a redundant tensor. It should get trimmed
      gt = tf.placeholder(tf.float32, shape=[None,10])

      W = tf.Variable(tf.ones([20,10]))
      b = tf.Variable(tf.ones([10]))

      y = tf.matmul(x,W) + b
      output_name = [y.op.name]
    # not batched
    self._test_tf_model(graph, {"test_linear/input:0":[1,20]},
        output_name, delta=1e-2)
    # batched
    self._test_tf_model(graph, {"test_linear/input:0":[8,20]},
        output_name, delta=1e-2)

  def test_log(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      # placeholder constructor returns a tensor not an op
      x = tf.placeholder(tf.float32, shape=[None,20], name="test_log/input")
      # Make a redundant tensor. It should get trimmed
      gt = tf.placeholder(tf.float32, shape=[None,10])

      W = tf.Variable(tf.ones([20,10]))
      b = tf.Variable(tf.ones([10]))

      y = tf.log(tf.matmul(x,W) + b)
      output_name = [y.op.name]

    self._test_tf_model(graph, {"test_log/input:0":[1,20]},
        output_name, delta=1e-2)

  def test_simple_convnet(self):
    def weight_variable(shape):
      initial = tf.truncated_normal(shape, stddev=0.1)
      return tf.Variable(initial)

    def bias_variable(shape):
      initial = tf.constant(0.1, shape=shape)
      return tf.Variable(initial)

    def conv2d(x, W):
      return tf.nn.conv2d(x, W, strides=[1, 1, 1, 1], padding='SAME')

    def max_pool_2x2(x):
      return tf.nn.max_pool(x, ksize=[1, 2, 2, 1],
                            strides=[1, 2, 2, 1], padding='SAME')

    graph = tf.Graph()
    with graph.as_default() as g:
      W_conv1 = weight_variable([5, 5, 1, 32])
      b_conv1 = bias_variable([32])

      x_image = tf.placeholder(tf.float32, shape=[None,28,28,1],
          name="test_simple_conv/input")
      h_conv1 = tf.nn.relu(conv2d(x_image, W_conv1) + b_conv1)
      h_pool1 = max_pool_2x2(h_conv1)

      W_conv2 = weight_variable([5, 5, 32, 64])
      b_conv2 = bias_variable([64])

      h_conv2 = tf.nn.relu(conv2d(h_pool1, W_conv2) + b_conv2)
      h_pool2 = max_pool_2x2(h_conv2)

    output_name = [h_pool2.op.name]
    self._test_tf_model(graph,
        {"test_simple_conv/input:0":[1,28,28,1]},
        output_name, delta=1e-2)

  def test_convnet(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_convnet/input")
      W_conv1 = tf.Variable(tf.truncated_normal([3,3,3,2], stddev=0.3))
      h_conv1 = tf.nn.conv2d(x_image,W_conv1, strides=[1,1,1,1], padding='SAME')
      h_conv1_flat = tf.reshape(h_conv1, [-1, 8*8*2])
      W_fc1 = tf.Variable(tf.truncated_normal([8*8*2,4], stddev=0.3))
      h_fc1 = tf.matmul(h_conv1_flat, W_fc1)

    output_name = [h_fc1.op.name]
    # not batched
    self._test_tf_model(graph,
        {"test_convnet/input:0":[1,8,8,3]}, output_name, delta=1e-2)
    # batched
    self._test_tf_model(graph,
        {"test_convnet/input:0":[10,8,8,3]}, output_name, delta=1e-2)

  def test_reduce_max(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      # placeholder constructor returns a tensor not an op
      x = tf.placeholder(tf.float32, shape=[None,20], 
          name="test_reduce_max/input")
      W = tf.Variable(tf.ones([20,10]))
      y = tf.matmul(x,W)
      output = tf.reduce_max(y, axis=-1)
      output_name = [output.op.name]
    # not batched
    self._test_tf_model(graph, {"test_reduce_max/input:0":[1,20]},
        output_name, delta=1e-2)

class TFSingleLayersTest(TFNetworkTest):
  """ Small models from tensorflow.layers
  """
  def test_dense(self):
    # dense layer with some activation
    graph = tf.Graph()
    with graph.as_default() as g:
      x = tf.placeholder(tf.float32, shape=[None,10],
          name="test_dense/input")
      y = tf.layers.dense(inputs=x, units=16, activation=tf.sigmoid)

    output_name = [y.op.name]
    self._test_tf_model(graph,
        {"test_dense/input:0":[1,10]}, output_name, delta=1e-2)

  def test_conv2d(self):
    # conv layer with "fused activation"
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2d/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[5,5],
          padding='same', activation=tf.nn.relu)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2d_valid(self):
    # conv layer with "fused activation"
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2d_valid/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='valid', activation=tf.nn.relu)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_valid/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2d_stride2(self):
    # conv layer with "fused activation"
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2d_stride2/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='valid', strides=(2,2))

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_stride2/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2d_dilated(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,32,32,3],
          name="test_conv2d_dilated/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='valid', dilation_rate=(3,4))

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_dilated/input:0":[1,32,32,3]}, output_name, delta=1e-2)

  def test_conv2dt(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2dt/input")
      conv1 = tf.layers.conv2d_transpose(inputs=x_image, filters=4,
          kernel_size=[3,3], padding='same', activation=tf.nn.relu)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2dt/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2dt_valid(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2dt_valid/input")
      conv1 = tf.layers.conv2d_transpose(inputs=x_image, filters=4,
          kernel_size=[3,3], padding='valid', activation=tf.nn.relu)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2dt_valid/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2dt_stride2(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_conv2dt_stride2/input")
      conv1 = tf.layers.conv2d_transpose(inputs=x_image, filters=4,
          kernel_size=[3,3], padding='valid', strides=(2,2))

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv2dt_stride2/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv2d_avepool(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name="test_conv2d_avepool/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='same', activation=tf.nn.relu)
      pool1 = tf.layers.average_pooling2d(inputs=conv1, pool_size=[2, 2],
          strides=2)

    output_name = [pool1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_avepool/input:0":[1,16,16,3]}, output_name, delta=1e-2)

  def test_conv2d_maxpool(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name="test_conv2d_maxpool/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='same', activation=tf.nn.relu)
      pool1 = tf.layers.max_pooling2d(inputs=conv1, pool_size=[3, 3], strides=1,
          padding='same')

    output_name = [pool1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_maxpool/input:0":[1,16,16,3]}, output_name, delta=1e-2)

  def test_conv2d_bn(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name="test_conv2d_bn/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=4, kernel_size=[3,3],
          padding='same', activation=tf.nn.relu)
      bn1 = tf.layers.batch_normalization(inputs=conv1, axis=-1)

    output_name = [bn1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_bn/input:0":[1,16,16,3]}, output_name, delta=1e-2)

  def test_conv2d_spatial_bn(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name="test_conv2d_bn/input")
      bn1 = tf.layers.batch_normalization(inputs=x_image, axis=2)

    output_name = [bn1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_bn/input:0":[1,16,16,3]}, output_name, delta=1e-2)

  def test_separable_conv2d(self):
    # conv layer with "fused activation"
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name="test_separable_conv2d/input")
      conv1 = tf.layers.separable_conv2d(inputs=x_image, filters=4,
          kernel_size=[3,3], padding='valid', depth_multiplier=2)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_separable_conv2d/input:0":[1,8,8,3]}, output_name, delta=1e-2)

  def test_conv1d(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,3],
          name="test_conv1d/input")
      conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=3,
          padding='valid', use_bias=True)

    output_name = [conv1.op.name]
    self._test_tf_model(graph,
        {"test_conv1d/input:0":[1,8,3]}, output_name, data_mode='linear',
        delta=.05)

  def test_conv1d_dense(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,3],
          name="test_conv1d_dense/input")
      conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=3,
          padding='same')
      conv1_flat = tf.reshape(conv1,[-1,8*2])
      y = tf.layers.dense(inputs=conv1_flat, units=6, activation=tf.nn.relu)

    output_name = [y.op.name]
    # not batched
    self._test_tf_model(graph,
        {"test_conv1d_dense/input:0":[1,8,3]}, output_name, delta=1e-2)
    # batched
    self._test_tf_model(graph,
        {"test_conv1d_dense/input:0":[10,8,3]}, output_name, delta=1e-2)

  def test_conv1d_avepool(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,3],
          name="test_conv1d_avepool/input")
      conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=5,
          padding='same')
      pool1 = tf.layers.average_pooling1d(inputs=conv1, pool_size=2,
          strides=2)

    output_name = [pool1.op.name]
    self._test_tf_model(graph,
        {"test_conv1d_avepool/input:0":[1,8,3]}, output_name, delta=1e-2)

  def test_conv1d_maxpool(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,8,3],
          name="test_conv1d_maxpool/input")
      conv1 = tf.layers.conv1d(inputs=x_image, filters=2, kernel_size=3,
          padding='same')
      pool1 = tf.layers.max_pooling1d(inputs=conv1, pool_size=2,
          strides=1)

    output_name = [pool1.op.name]
    self._test_tf_model(graph,
        {"test_conv1d_maxpool/input:0":[1,8,3]}, output_name, delta=1e-2)

  def test_conv2d_resize_bilinear(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name="test_conv2d_resize_bl/input")
      conv1 = tf.layers.conv2d(inputs=x_image, filters=3, kernel_size=[3,3],
          padding='same', activation=tf.nn.relu)
      bl1 = tf.image.resize_bilinear(images=conv1, size=[32,32])

    output_name = [bl1.op.name]
    self._test_tf_model(graph,
        {"test_conv2d_resize_bl/input:0":[1,16,16,3]}, output_name, delta=1e-2)

  def test_concat_constants(self):
    graph = tf.Graph()
    x, y = np.meshgrid(np.linspace(0., 1., 256), np.linspace(0., 1., 256))
    x = np.reshape(x, [1, 256, 256, 1])
    y = np.reshape(y, [1, 256, 256, 1])
    with graph.as_default() as g:
      x_image = tf.placeholder(tf.float32, shape=[None, 256, 256, 3],
                               name="input_image")
      xx = tf.constant(x, dtype=tf.float32)
      yy = tf.constant(y, dtype=tf.float32)
      img_concatenated = tf.concat([x_image, xx, yy], -1, name='concat')

    output_name = [img_concatenated.op.name]
    self._test_tf_model_constant(graph,
        {"input_image:0": [1, 256, 256, 3]}, output_name, delta=1e-2)


class TFSlimTest(TFNetworkTest):
  """Small models for tf.slim layers
  """
  def test_slim_stacked_conv2d(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_stacked_conv2d/input')
      with slim.arg_scope([slim.conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.conv2d(inputs, 2, [5, 5], scope='conv1')
        net = slim.conv2d(net, 4, [3, 3], padding='VALID', scope='conv2')
        net = slim.conv2d(net, 8, [3, 3], scope='conv3')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_stacked_conv2d/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_repeat(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_repeat/input')
      with slim.arg_scope([slim.conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.repeat(inputs, 2, slim.conv2d, 64, [3, 3], scope='conv1')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_repeat/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_fc(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,8],
          name='test_slim_vgg_fc/input')
      with slim.arg_scope([slim.fully_connected],
          weights_initializer=tf.truncated_normal_initializer(0.0, 0.2),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.fully_connected(inputs, 10, scope='fc')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_vgg_fc/input:0":[1,8]},
        output_name, delta=1e-2)

  def test_slim_convnet(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,8,8,3],
          name='test_slim_convnet/input')
      with slim.arg_scope([slim.conv2d, slim.fully_connected],
          weights_initializer=tf.truncated_normal_initializer(0.0, 0.2),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.conv2d(inputs, 2, [3, 3], scope='conv1')
        net = slim.flatten(net, scope='flatten3')
        net = slim.fully_connected(net, 6, scope='fc6')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_convnet/input:0":[1,8,8,3]},
        output_name, delta=1e-2)

  def test_slim_lenet(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,28,28,1],
          name='test_slim_lenet/input')
      net = slim.conv2d(inputs, 4, [5,5], scope='conv1')
      net = slim.avg_pool2d(net, [2,2], scope='pool1')
      net = slim.conv2d(net, 6, [5,5], scope='conv2')
      net = slim.max_pool2d(net, [2,2], scope='pool2')
      net = slim.flatten(net, scope='flatten3')
      net = slim.fully_connected(net, 10, scope='fc4')
      net = slim.fully_connected(net, 10, activation_fn=None, scope='fc5')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_lenet/input:0":[1,28,28,1]},
        output_name, delta=1e-2)

  def test_slim_one_hot(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      # input is usually a known / unknown batch size
      inputs = tf.placeholder(tf.int64, shape=[None],
          name='test_slim_one_hot/input')
      net = slim.one_hot_encoding(inputs, 10)
      net = slim.fully_connected(net, 6)
    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_one_hot/input:0":[3]},
        output_name, delta=1e-2, data_mode='linear',
        one_dim_seq_flags=[True])

  def test_slim_conv_bn(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_conv2d_bn/input')
      with slim.arg_scope([slim.conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.conv2d(inputs, 2, [5, 5], scope='conv1')
        net = slim.batch_norm(net, center=True, scale=True, is_training=False)
    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_conv2d_bn/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_conv_bn_no_beta(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_conv_bn_no_beta/input')
      with slim.arg_scope([slim.conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.conv2d(inputs, 2, [5, 5], scope='conv1')
        net = slim.batch_norm(net, center=False, scale=False, is_training=False)
    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_conv_bn_no_beta/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_separable_conv(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_separable_conv2d/input')
      with slim.arg_scope([slim.separable_conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3)):
        net = slim.separable_conv2d(inputs, 2, [5, 5], 2, scope='conv1')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_separable_conv2d/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_dilated_depthwise_conv(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_separable_conv2d/input')
      with slim.arg_scope([slim.separable_conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3)):
        net = slim.separable_conv2d(inputs,
            num_outputs=None,
            stride=1,
            depth_multiplier=1,
            kernel_size=[3, 3],
            rate=2,
            scope='conv1')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_separable_conv2d/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  def test_slim_deconv(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_decconv2d/input')
      with slim.arg_scope([slim.separable_conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3)):
        net = slim.conv2d_transpose(inputs, 2, [3, 3], scope='conv1')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_decconv2d/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  # TODO - this fails due to unsupported op "Tile"
  @unittest.skip
  def test_slim_plane_conv(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,16,16,3],
          name='test_slim_plane_conv2d/input')
      with slim.arg_scope([slim.separable_conv2d], padding='SAME',
          weights_initializer=tf.truncated_normal_initializer(stddev=0.3)):
        net = slim.conv2d_in_plane(inputs, 2, [3, 3], scope='conv1')

    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_plane_conv2d/input:0":[1,16,16,3]},
        output_name, delta=1e-2)

  # TODO - this fails due to unsupported op "Tile"
  @unittest.skip
  def test_slim_unit_norm(self):
    graph = tf.Graph()
    with graph.as_default() as g:
      inputs = tf.placeholder(tf.float32, shape=[None,8],
          name='test_slim_unit_norm/input')
      with slim.arg_scope([slim.fully_connected],
          weights_initializer=tf.truncated_normal_initializer(0.0, 0.2),
          weights_regularizer=slim.l2_regularizer(0.0005)):
        net = slim.fully_connected(inputs, 10, scope='fc')
        net = slim.unit_norm(net,1)
    output_name = [net.op.name]
    self._test_tf_model(graph,
        {"test_slim_unit_norm/input:0":[1,8]},
        output_name, delta=1e-2)


