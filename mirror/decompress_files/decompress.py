# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
r"""Decompress scan files automatically on create.

Automatically decompress files in the gs://firehook-censoredplanetscanspublic/
bucket into the gs://firehook-scans/ bucket.

Files in the compressed bucket are of the form
gs://firehook-censoredplanetscanspublic/CP_Quack-discard-2018-07-28-03-11-21.tar.gz
or
gs://firehook-censoredplanetscanspublic/CP_Satellite-2018-08-07-17-24-41.tar.gz

They should be decompressed into scan type specific directories like
gs://firehook-scans/discard/CP_Quack-discard-2018-07-28-03-11-21/results.json
or
gs://firehook-scans/satellite/CP_Satellite-2018-08-07-17-24-41/results.json
"""

import os
from pprint import pprint
import shutil
import tarfile

import requests
from retry import retry

from google.cloud import storage

PROJECT_NAME = 'firehook-censoredplanet'
COMPRESSED_BUCKET_NAME = 'firehook-censoredplanetscanspublic'
UNCOMPRESSED_BUCKET_NAME = 'firehook-scans'

timeout_5_minutes = 300

client = storage.Client(project=PROJECT_NAME)
compressed_bucket = client.get_bucket(COMPRESSED_BUCKET_NAME)
uncompressed_bucket = client.get_bucket(UNCOMPRESSED_BUCKET_NAME)

# Key - a substring that will be in the filename
# Value - the scan type of that file
scan_type_identifiers = {
    'Satellite-': 'satellite',
    'Quack-echo-': 'echo',
    'Quack-discard-': 'discard',
    'Quack-https-': 'https',
    'Quack-http-': 'http'
}


@retry(requests.exceptions.ConnectionError, tries=3, delay=1)
def decompress_file(tar_name):
  """Decompress a given scan file.

  Downloads the file from GCS, decompresses in memory,
  and uploads the decompressed version to a different location in GCS.

  Args:
    tar_name: filename like CP_Quack-discard-2020-08-17-08-41-15.tar.gz

  Raises:
    Exception: when the file has an unknown scan type
  """
  scan_type = None
  for type_identifier, potential_scan_type in scan_type_identifiers.items():
    if type_identifier in tar_name:
      scan_type = potential_scan_type

  if not scan_type:
    raise Exception("Couldn't determine scan type for filename " + tar_name)

  tmp_filepath = os.path.join('/tmp', tar_name)
  tar_folder = tar_name[:-7]  # remove the extensions
  tmp_folder = os.path.join('/tmp', tar_folder)

  compressed_bucket.get_blob(tar_name).download_to_filename(
      tmp_filepath, timeout=timeout_5_minutes)

  tfile = tarfile.open(tmp_filepath, 'r:gz')
  tfile.extractall('/tmp')

  for filename in os.listdir(tmp_folder):
    filepath = os.path.join(tmp_folder, filename)

    if os.path.isfile(filepath):
      output_blob = uncompressed_bucket.blob(
          os.path.join(scan_type, tar_folder, filename))
      output_blob.upload_from_filename(filepath, timeout=timeout_5_minutes)

  os.remove(tmp_filepath)
  shutil.rmtree(tmp_folder)


def get_all_compressed_filenames():
  """Get a list of all compressed filenames, minus the file extension.

  Returns:
    a list of filename strings
    ex ["CP_Quack-discard-2020-08-17-08-41-15",
        "CP_Satellite-2020-08-16-17-07-54"]
  """
  # CP_Satellite-2020-08-16-17-07-54.tar.gz
  blobs = list(client.list_blobs(COMPRESSED_BUCKET_NAME))
  # CP_Satellite-2020-08-16-17-07-54
  filenames = [  # remove both .tar and .gz
      os.path.splitext(os.path.splitext(blob.name)[0])[0] for blob in blobs
  ]
  return filenames


def get_all_uncompressed_filepaths():
  """Get a list of all directories with uncompressed filenames.

  Returns:
    a list of filename strings
    ex ["CP_Quack-discard-2020-08-17-08-41-15",
        "CP_Satellite-2020-08-16-17-07-54"]
  """
  # discard/CP_Quack-discard-2020-08-17-08-41-15/results.json
  blobs = list(client.list_blobs(UNCOMPRESSED_BUCKET_NAME))
  # discard/CP_Quack-discard-2020-08-17-08-41-15/
  paths = [os.path.split(blob.name)[0] for blob in blobs]
  # CP_Quack-discard-2020-08-17-08-41-15/
  path_ends = [os.path.split(path)[1] for path in paths]
  return path_ends


def get_missing_compressed_files(compressed_files, uncompressed_files):
  """Get all files in the compressed list that are not in the uncompressed list."""
  diff = set(compressed_files) - set(uncompressed_files)
  return list(diff)


def decompress_all_missing_files():
  """Decompress all files that exist only in the compressed bucket.

  Used for backfilling data.
  """
  compressed_files = get_all_compressed_filenames()
  uncompressed_files = get_all_uncompressed_filepaths()
  new_files = get_missing_compressed_files(compressed_files, uncompressed_files)

  files_with_extensions = [filename + '.tar.gz' for filename in new_files]

  if not files_with_extensions:
    pprint('no new scan files to decompress')

  for filename in files_with_extensions:
    pprint(('decompressing file: ', filename))
    decompress_file(filename)
    pprint(('decompressed file: ', filename))


if __name__ == '__main__':
  # Called manually when running a backfill.
  decompress_all_missing_files()