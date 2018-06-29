# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Simple image classification with Inception.

Run image classification with Inception trained on ImageNet 2012 Challenge data
set.

This program creates a graph from a saved GraphDef protocol buffer,
and runs inference on an input JPEG image. It outputs human readable
strings of the top 5 predictions along with their probabilities.

Change the --warm_up_image_file argument to specify any jpg image to warm-up 
the TensorFlow model.

Please see the tutorial and website for a detailed description of how
to use this script to perform image recognition.

https://tensorflow.org/tutorials/image_recognition/
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os.path
import re
import sys
import tarfile

import numpy as np
from six.moves import urllib
import tensorflow as tf

import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl

FLAGS = tf.app.flags.FLAGS

# global variables used to prevent TensorFlow from initializing for multi times
sess = tf.Session()
softmax_tensor = None

# classify_image_graph_def.pb:
#   Binary representation of the GraphDef protocol buffer.
# imagenet_synset_to_human_label_map.txt:
#   Map from synset ID to a human readable string.
# imagenet_2012_challenge_label_map_proto.pbtxt:
#   Text representation of a protocol buffer mapping a label to synset ID.
tf.app.flags.DEFINE_string(
    'model_dir', '/tmp/imagenet',
    """Path to classify_image_graph_def.pb, """
    """imagenet_synset_to_human_label_map.txt, and """
    """imagenet_2012_challenge_label_map_proto.pbtxt.""")
tf.app.flags.DEFINE_string('warm_up_image_file', '',
                           """Absolute path to warm-up image file.""")
tf.app.flags.DEFINE_integer('num_top_predictions', 5,
                            """Display this many predictions.""")
tf.app.flags.DEFINE_string('httpd_bind_ip', '0.0.0.0',
                           """Specify alternate httpd bind address. """
                           """By default, httpd will bind to all interfaces of network cards on this machine.""")
tf.app.flags.DEFINE_integer('httpd_bind_port', 8080,
                            """Specify alternate httpd bind port.""")

# pylint: disable=line-too-long
DATA_URL = 'http://download.tensorflow.org/models/image/imagenet/inception-2015-12-05.tgz'
# pylint: enable=line-too-long


class MyRequestHandler(BaseHTTPRequestHandler):
  def do_GET(self):
    # e.g. "/?image_path=/root/mobike.jpg"
    path = self.path
    # e.g. "/root/mobike.jpg"
    image_path = parse_qsl(path[2:])[0][1]
    print('-------------------------------------------')
    print('Will process image: {}\n'.format(image_path))

    prediction_result = run_inference_on_image(image_path)
    message_return_to_client = ''
    for one_line in prediction_result:
      message_return_to_client = message_return_to_client + one_line + '\r\n'

    # send response status code
    self.send_response(200)
    
    # send headers
    self.send_header('Content-type','text/plain; charset=utf-8')
    self.end_headers()
    
    # send message back to client, write content as utf-8 data
    self.wfile.write(bytes(message_return_to_client, "utf8"))
    print('Process image {} done\n'.format(image_path))
    return
    

class NodeLookup(object):
  """Converts integer node ID's to human readable labels."""

  def __init__(self,
               label_lookup_path=None,
               uid_lookup_path=None):
    if not label_lookup_path:
      label_lookup_path = os.path.join(
          FLAGS.model_dir, 'imagenet_2012_challenge_label_map_proto.pbtxt')
    if not uid_lookup_path:
      uid_lookup_path = os.path.join(
          FLAGS.model_dir, 'imagenet_synset_to_human_label_map.txt')
    self.node_lookup = self.load(label_lookup_path, uid_lookup_path)

  def load(self, label_lookup_path, uid_lookup_path):
    """Loads a human readable English name for each softmax node.

    Args:
      label_lookup_path: string UID to integer node ID.
      uid_lookup_path: string UID to human-readable string.

    Returns:
      dict from integer node ID to human-readable string.
    """
    if not tf.gfile.Exists(uid_lookup_path):
      tf.logging.fatal('File does not exist %s', uid_lookup_path)
    if not tf.gfile.Exists(label_lookup_path):
      tf.logging.fatal('File does not exist %s', label_lookup_path)

    # Loads mapping from string UID to human-readable string
    proto_as_ascii_lines = tf.gfile.GFile(uid_lookup_path).readlines()
    uid_to_human = {}
    p = re.compile(r'[n\d]*[ \S,]*')
    for line in proto_as_ascii_lines:
      parsed_items = p.findall(line)
      uid = parsed_items[0]
      human_string = parsed_items[2]
      uid_to_human[uid] = human_string

    # Loads mapping from string UID to integer node ID.
    node_id_to_uid = {}
    proto_as_ascii = tf.gfile.GFile(label_lookup_path).readlines()
    for line in proto_as_ascii:
      if line.startswith('  target_class:'):
        target_class = int(line.split(': ')[1])
      if line.startswith('  target_class_string:'):
        target_class_string = line.split(': ')[1]
        node_id_to_uid[target_class] = target_class_string[1:-2]

    # Loads the final mapping of integer node ID to human-readable string
    node_id_to_name = {}
    for key, val in node_id_to_uid.items():
      if val not in uid_to_human:
        tf.logging.fatal('Failed to locate: %s', val)
      name = uid_to_human[val]
      node_id_to_name[key] = name

    return node_id_to_name

  def id_to_string(self, node_id):
    if node_id not in self.node_lookup:
      return ''
    return self.node_lookup[node_id]


def create_graph():
  """Creates a graph from saved GraphDef file and returns a saver."""
  # Creates graph from saved graph_def.pb.
  with tf.gfile.FastGFile(os.path.join(
      FLAGS.model_dir, 'classify_image_graph_def.pb'), 'rb') as f:
    graph_def = tf.GraphDef()
    graph_def.ParseFromString(f.read())
    _ = tf.import_graph_def(graph_def, name='')


def warm_up_model(image):
  """Warm-up TensorFlow model, to increase the inference speed of each time."""

  # the image used to warm-up TensorFlow model
  if not tf.gfile.Exists(image):
    tf.logging.fatal('File does not exist %s', image)
  image_data = tf.gfile.FastGFile(image, 'rb').read()

  # Creates graph from saved GraphDef.
  create_graph()

  global sess, softmax_tensor
  softmax_tensor = sess.graph.get_tensor_by_name('softmax:0')

  print('Warm-up start')
  for i in range(10):
    print('Warm-up for time {}'.format(i))
    predictions = sess.run(softmax_tensor, {'DecodeJpeg/contents:0': image_data})

  print('Warm-up finished')


def run_inference_on_image(image):
  """Runs inference on an image.

  Args:
    image: Image file name.

  Returns:
    Nothing
  """
  if not tf.gfile.Exists(image):
    tf.logging.fatal('File does not exist %s', image)
  image_data = tf.gfile.FastGFile(image, 'rb').read()

  # Some useful tensors:
  # 'softmax:0': A tensor containing the normalized prediction across
  #   1000 labels.
  # 'pool_3:0': A tensor containing the next-to-last layer containing 2048
  #   float description of the image.
  # 'DecodeJpeg/contents:0': A tensor containing a string providing JPEG
  #   encoding of the image.
  # Runs the softmax tensor by feeding the image_data as input to the graph.
  global softmax_tensor, sess

  # record the start time of the actual prediction
  start_time = time.time()

  predictions = sess.run(softmax_tensor,
                         {'DecodeJpeg/contents:0': image_data})
  predictions = np.squeeze(predictions)

  # Creates node ID --> English string lookup.
  node_lookup = NodeLookup()

  # a list which contains the content to return to HTTP client
  prediction_result = []
  top_k = predictions.argsort()[-FLAGS.num_top_predictions:][::-1]
  for node_id in top_k:
    human_string = node_lookup.id_to_string(node_id)
    score = predictions[node_id]
    prediction_result.append('%s (score = %.5f)' % (human_string, score))

  prediction_result.append('Prediction used time:{} Seconds'.format(time.time() - start_time))
  return prediction_result


class DownloadError(RuntimeError):
  pass


def download_model(url: str, dst_dir: str = None) -> str:
  """Download model tarball from URL into destination directory

  :param url:
  :param dst_dir:
  :returns downloaded filename
  """
  import click

  if not is_valid_url(url):
    raise DownloadError('URL {0} is not valid'.format(url))
  filename = url.split('/')[-1]
  if not dst_dir:
    dst_dir = os.path.abspath('.')
  if not os.path.isdir(dst_dir):
    raise DownloadError('{0} is not a valid directory!'.format(dst_dir))
  path_to_filename = os.path.join(dst_dir, filename)
  click.echo('Downloading {0} from {1} ...'.format(filename, url))
  bar_len = 1000
  with click.progressbar(length=bar_len) as bar:
    def _progress(count, block_size, total_size):
      percent = int(count * block_size * bar_len / total_size)
      if 0 < percent < bar_len:  # Hack because we can't set the position
        bar.pos = percent
        bar.update(0)

    try:
      urllib.request.urlretrieve(url, os.path.join(dst_dir, filename), _progress)
      bar.update(bar_len)
    except (urllib.error.URLError, ConnectionAbortedError) as e:
      raise DownloadError('Download failed: {0}'.format(e))
  if not os.path.exists(path_to_filename):
    raise DownloadError('Download failed: {0} do not exist.'.format(path_to_filename))
  return path_to_filename


def is_valid_url(url: str):
  return True  # TODO: Implement URL format checking...


def is_extracted_model_dir(directory: str) -> bool:
  """Check whether model files are valid or not"""
  excepted_files = {
    'classify_image_graph_def.pb',
    'imagenet_2012_challenge_label_map_proto.pbtxt',
    'imagenet_synset_to_human_label_map.txt'
  }
  for filename in excepted_files:
    filepath = os.path.join(directory, filename)
    if not os.path.exists(filepath):
      return False
  # TODO: In the future, we may check the data format of each file.
  return True


def maybe_download_and_extract():
  """Download and extract model tar file."""
  dest_directory = FLAGS.model_dir
  if is_extracted_model_dir(dest_directory):
    return
  if not os.path.exists(dest_directory):
    os.makedirs(dest_directory)
  elif not os.path.isdir(dest_directory):
    raise RuntimeError('%s exists but it is NOT a directory!' % dest_directory)
  filename = DATA_URL.split('/')[-1]
  filepath = os.path.join(dest_directory, filename)
  downloaded = download_model(DATA_URL, dest_directory) if not os.path.exists(filepath) else filepath
  tarfile_extracted = False
  MAX_RETRY = 2
  cnt = 0
  while not tarfile_extracted and cnt < MAX_RETRY:
    try:
      with tarfile.open(downloaded, 'r:gz') as t:
        t.extractall(dest_directory)
        tarfile_extracted = True
        break
    except EOFError as e:
      print('Error: File "%s" is broken: %s' % (downloaded, e))
    except tarfile.TarError as e:
      print('Error: Can not unzip download file %s: %s' % (downloaded, e))
    cnt += 1
    print('Retrying...')
    downloaded = download_model(DATA_URL, dest_directory)
  if not tarfile_extracted:
    raise RuntimeError('Failed to retrieve model files from %s!' % DATA_URL)


def main(_):
  maybe_download_and_extract()

  if not FLAGS.warm_up_image_file or not os.path.exists(FLAGS.warm_up_image_file):
    # Fail-save to use default image file when --warm_up_image_file do not exists
    FLAGS.warm_up_image_file = os.path.join(os.path.abspath(FLAGS.model_dir), 'cropped_panda.jpg')
    print('Using default image file', FLAGS.warm_up_image_file, 'to warm up model...')
  warm_up_model(FLAGS.warm_up_image_file)

  server_address = (FLAGS.httpd_bind_ip, FLAGS.httpd_bind_port)
  httpd = HTTPServer(server_address, MyRequestHandler)
  print('TensorFlow service started')
  httpd.serve_forever()


if __name__ == '__main__':
  tf.app.run()
