"""Beam pipeline for converting json scan files into bigquery tables."""

from __future__ import absolute_import

import datetime
import json
import logging
import re
from pprint import pprint
from typing import Optional, Tuple, Dict, List

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io.gcp.internal.clients import bigquery
from apache_beam.io.gcp.gcsfilesystem import GCSFileSystem
from apache_beam.io.gcp.gcsio import GcsIO
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions

bigquery_schema = {
    # Columns from Censored Planet data
    'domain': 'string',
    'ip': 'string',
    'date': 'date',
    'start_time': 'timestamp',
    'end_time': 'timestamp',
    'retries': 'integer',
    'sent': 'string',
    'received': 'string',
    'error': 'string',
    'blocked': 'boolean',
    'success': 'boolean',
    'fail_sanity': 'boolean',
    'stateful_block': 'boolean',
    # Columns added from CAIDA data
    'netblock': 'string',
    'asn': 'integer',
    'as_name': 'string',
    'as_full_name': 'string',
    'country': 'string',
}
# Future fields
"""
    'row_number', 'integer',
    'domain_category': 'string',
    'as_traffic': 'integer',
    'as_class': 'string',
"""


def get_bigquery_schema() -> bigquery.TableSchema:
  """Return a beam bigquery schema for the output table."""
  table_schema = bigquery.TableSchema()

  for (name, field_type) in bigquery_schema.items():

    field_schema = bigquery.TableFieldSchema()
    field_schema.name = name
    field_schema.type = field_type
    field_schema.mode = 'nullable'  # all fields are flat
    table_schema.fields.append(field_schema)

  return table_schema


def read_scan_text(p: beam.Pipeline,
                   gcs: GCSFileSystem,
                   scan_type: str,
                   start_date: Optional[datetime.date] = None,
                   end_date: Optional[datetime.date] = None):
  """Read in the given json files for a date segment.

  Args:
    p: beam pipeline object
    gcs: GCSFileSystem object
    scan_type: one of "echo", "discard", "http", "https"
    start_date: date object, only files after or at this date will be read
    end_date: date object, only files at or before this date will be read

  Returns:
    A PCollection[Tuple[filename, line]]
    of all the lines in the files keyed by filename
  """
  filename_regex = 'gs://firehook-scans/' + scan_type + '/**/results.json'
  file_metadata = [m.metadata_list for m in gcs.match([filename_regex])][0]
  filenames = [metadata.path for metadata in file_metadata]
  filtered_filenames = [
      filename for filename in filenames
      if between_dates(filename, start_date, end_date)
  ]

  # List[PCollection[line]]
  multiple_file_lines = [
      p
      | 'read file ' + filename >> ReadFromText(filename)
      for filename in filtered_filenames
  ]

  # List[PCollection[Tuple[filename,line]]]
  multiple_file_lines_with_filenames = []
  for (file_lines, filename) in zip(multiple_file_lines, filtered_filenames):
    step_name = 'annotate_filename ' + filename

    # PCollection[Tuple[filename,line]]
    file_lines_with_filenames = (
        file_lines | step_name >> beam.Map(
            make_tuple, filename).with_output_types(Tuple[str, str]))

    multiple_file_lines_with_filenames.append(file_lines_with_filenames)

  # PCollection[Tuple[filename,line]]
  lines = (
      tuple(multiple_file_lines_with_filenames)
      | beam.Flatten().with_output_types(Tuple[str, str]))
  return lines


def make_tuple(line: str, filename: str) -> Tuple[str, str]:
  """Helper method for making a tuple from two args."""
  return (filename, line)


def between_dates(filename: str,
                  start_date: Optional[datetime.date] = None,
                  end_date: Optional[datetime.date] = None) -> bool:
  """Return true if a filename is between (or matches either of) two dates.

  Args:
    filename: string of the format
    "gs://firehook-scans/http/CP_Quack-http-2020-05-11-01-02-08/results.json"
    start_date: date object or None
    end_date: date object or None

  Returns:
    boolean
  """
  date = datetime.date.fromisoformat(
      re.findall(r'\d\d\d\d-\d\d-\d\d', filename)[0])
  if start_date and end_date:
    return start_date <= date <= end_date
  elif start_date:
    return start_date <= date
  elif end_date:
    return date <= end_date
  else:
    return True


def flatten_measurement(filename: str, line: str) -> Tuple[str, Dict[str, str]]:
  """Flatten a measurement string into several roundtrip rows.

  Args:
    filename: a filepath string
    line: a json string describing a censored planet measurement. example
    {'Keyword': 'test.com,
     'Server': '1.2.3.4',
     'Results': [{'Success': true},
                 {'Success': false}]}

  Returns:
    An array of dicts containing individual roundtrip information
    Keyed by YYYY-MM-dd date string
    [(date, {'column_name': field_value})]
    example
    [('2020-01-01', {'domain': 'test.com', 'ip': '1.2.3.4', 'success': true})
     ('2020-01-02', {'domain': 'test.com', 'ip': '1.2.3.4', 'success': true})]
  """
  rows = []

  try:
    scan = json.loads(line)
  except json.decoder.JSONDecodeError as e:
    logging.error('JSONDecodeError: %s\nFilename: %s\n%s\n', e, filename, line)
    return rows

  for result in scan['Results']:
    received = result.get('Received', '')
    if isinstance(received, str):
      received_flat = received
    else:
      # TODO figure out a better way to deal with the structure in http/https
      received_flat = json.dumps(received)

    date = result['StartTime'][:10]
    row = {
        'domain': scan['Keyword'],
        'ip': scan['Server'],
        'date': date,
        'start_time': result['StartTime'],
        'end_time': result['EndTime'],
        'retries': scan['Retries'],
        'sent': result['Sent'],
        'received': received_flat,
        'error': result.get('Error', ''),
        'blocked': scan['Blocked'],
        'success': result['Success'],
        'fail_sanity': scan['FailSanity'],
        'stateful_block': scan['StatefulBlock'],
    }
    rows.append((date, row))
  return rows


def add_ip_metadata(date, rows, gcs=None) -> Tuple[str, List[Dict[str, str]]]:
  """Add Autonymous System metadata for ips in the given rows.

  Args:
    date: a 'YYYYMMDD' date key
    rows: a list of dicts containing individual roundtrip information
    ex [{'domain': 'test.com', 'ip': '1.2.3.4', 'success': true}]
    gcs: GCSFileSystem

  Returns:
    a Tuple(date, list of the same row dicts with additional key/values added)
  """
  # this needs to be imported here
  # since this local class will be used on remote workers
  from metadata.ip_metadata import IpMetadata

  ip_metadata = IpMetadata(gcs, date)
  new_rows = []

  for row in rows:
    new_row = {}
    new_row.update(row)

    try:
      (netblock, asn, as_name, as_full_name,
       country) = ip_metadata.lookup(row['ip'])
      new_row.update({
          'netblock': netblock,
          'asn': asn,
          'as_name': as_name,
          'as_full_name': as_full_name,
          'country': country,
      })
    except KeyError as e:
      logging.error('KeyError: %s\n', e)

    new_rows.append(new_row)

  return (date, new_rows)


def run():
  pipeline_options = PipelineOptions(
      # DataflowRunner or DirectRunner
      runner='DataflowRunner',
      project='firehook-censoredplanet',
      region='us-east1',
      staging_location='gs://firehook-dataflow-test/staging',
      temp_location='gs://firehook-dataflow-test/temp',
      job_name='flatten-json-http-not-shuffled-job3',
      runtime_type_check=False,  # slow in prod
      setup_file='./setup.py')
  pipeline_options.view_as(SetupOptions).save_main_session = True
  gcs = GCSFileSystem(pipeline_options)

  with beam.Pipeline(options=pipeline_options) as p:
    scan_type = 'http'

    start_date = datetime.date.fromisoformat('2020-05-07')
    end_date = datetime.date.fromisoformat('2020-05-11')

    # PCollection[Tuple[filename,line]]
    lines = read_scan_text(
        p, gcs, scan_type, start_date=start_date, end_date=end_date)

    # PCollection[Tuple[date,Dict[column_name,field_value]]]
    rows = (
        lines | 'flatten json' >> beam.FlatMapTuple(
            flatten_measurement).with_output_types(Tuple[str, Dict[str, str]]))

    # PCollection[Tuple[date,List[Dict[column_name,field_value]]]]
    grouped_rows = (
        rows | 'group by dates' >> beam.GroupByKey().with_output_types(
            Tuple[str, List[Dict[str, str]]]))

    # Put GCS in a one-element pcollection so we can pass it remotely.
    gcs_pcoll = p | 'Create GCS Singleton' >> beam.Create([gcs])

    # PCollection[Tuple[date,List[Dict[column_name,field_value]]]]
    grouped_rows_with_metadata = (
        grouped_rows
        | 'add metadata' >> beam.MapTuple(
            add_ip_metadata, gcs=beam.pvalue.AsSingleton(
                gcs_pcoll)).with_output_types(Tuple[str, List[Dict[str, str]]]))

    # PCollection[Dict[column_name,field_value]]
    rows_with_metadata = (
        grouped_rows_with_metadata
        | 'flatten rows' >> beam.FlatMapTuple(
            lambda date, rows: rows).with_output_types(Dict[str, str]))

    bigquery_table = 'firehook-censoredplanet:' + scan_type + '_results.scan_test'

    rows_with_metadata | 'Write' >> beam.io.WriteToBigQuery(
        bigquery_table,
        schema=get_bigquery_schema(),
        create_disposition=beam.io.BigQueryDisposition.CREATE_IF_NEEDED,
        write_disposition=beam.io.BigQueryDisposition.WRITE_TRUNCATE)


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()