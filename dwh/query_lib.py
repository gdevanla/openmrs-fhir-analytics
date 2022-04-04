# Copyright 2021 Google LLC
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
"""This is the main higher level library to query FHIR resources.

The public interface of this library is intended to be independent of the actual
query engine, e.g., Spark, SQL/BigQuery, etc. The only exception is a single
function that defines the source of the data.
"""

# See https://stackoverflow.com/questions/33533148 why this is needed.
from __future__ import annotations
from enum import Enum
from typing import List, Any
import typing as tp
import pandas
from pyspark import SparkConf
from pyspark.sql import SparkSession, DataFrame
import pyspark.sql.functions as F
import pyspark.sql.types as T

try:
  from google.cloud import bigquery
except ImportError:
  pass # not all set up need to have bigquery libraries installed


import common

# This separator is used to merge date and values into one string.
DATE_VALUE_SEPARATOR = '_SeP_'


def merge_date_and_value(d: str, v: Any) -> str:
    return '{}{}{}'.format(d, DATE_VALUE_SEPARATOR, v)


def _build_in_list_with_quotes(values: tp.Iterable[tp.Any]):
    return ",".join(map(lambda x: '\"{}\"'.format(x), values))


class Runner(Enum):
    SPARK = 1
    BIG_QUERY = 2
    #FHIR_SERVER = 3


def patient_query_factory(runner: Runner,
                          data_source: str,
                          code_system: str = None) -> PatientQuery:
    """Returns the right instance of `PatientQuery` based on `data_source`.

  Args:
    runner: The runner to use for making data queries
    data_source: The definition of the source, e.g., directory containing
      Parquet files or a BigQuery dataset.

  Returns:
    The created instance.

  Raises:
    ValueError: When the input `data_source` is malformed or not implemented.
  """
  if runner == Runner.SPARK:
    return _SparkPatientQuery(data_source, code_system)
  if runner == Runner.BIG_QUERY:
    return _BigQueryPatientQuery(data_source, code_system)
  raise ValueError('Query engine {} is not supported yet.'.format(runner))


class _ObsConstraints():
  """ An abstraction layer around observation constraints for a single code.

  It is assumed that the conditions generated by the `sql` function is applied
  on an already flattened observation view.
  """

  def __init__(self, code: str, values: List[str] = None, value_sys: str = None,
      min_value: float = None, max_value: float = None,
      min_time: str = None, max_time: str = None) -> None:
    self._code = code
    self._sys_str = '="{}"'.format(value_sys) if value_sys else 'IS NULL'
    self._values = values
    self._min_time = min_time
    self._max_time = max_time
    self._min_value = min_value
    self._max_value = max_value

  @staticmethod
  def time_constraint(min_time: str = None, max_time: str = None):
    if not min_time and not max_time:
      return 'TRUE'
    cl = []
    if min_time:
      cl.append('dateTime >= "{}"'.format(min_time))
    if max_time:
      cl.append('dateTime <= "{}"'.format(max_time))
    return ' AND '.join(cl)

  def sql(self) -> str:
    """This creates a constraint string with WHERE syntax in SQL.

    All of the observation constraints specified by this instance are joined
    together into an `AND` clause.
    """
    cl = [self.time_constraint(self._min_time, self._max_time)]
    cl.append('coding.code="{}"'.format(self._code))
    # We don't need to filter coding.system as it is already done in flattening.
    if self._values:
      codes_str = ','.join(['"{}"'.format(v) for v in self._values])
      cl.append('value.codeableConcept.coding IN ({})'.format(codes_str))
      cl.append('value.codeableConcept.system {}'.format(self._sys_str))
    elif self._min_value or self._max_value:
      if self._min_value:
        cl.append(' value.quantity.value >= {} '.format(self._min_value))
      if self._max_value:
        cl.append(' value.quantity.value <= {} '.format(self._max_value))
    return '({})'.format(' AND '.join(cl))


class _EncounterContraints():
  """ An abstraction layer around all encounter constraints.

  It is assumed that the conditions generated by the `sql` function is applied
  on an already flattened encounter view.
  """

  def __init__(self, locationId: List[str] = None,
      typeSystem: str = None, typeCode: List[str] = None):
    self._location_id = locationId
    self._type_system = typeSystem
    self._type_code = typeCode

  def has_location(self) -> bool:
    return self._location_id != None

  def has_type(self) -> bool:
    return (self._type_code != None) or (self._type_system != None)

  def sql(self) -> str:
    """This creates a constraint string with WHERE syntax in SQL."""
    loc_str = 'TRUE'
    if self._location_id:
      temp_str = ','.join(['"{}"'.format(v) for v in self._location_id])
      loc_str = 'locationId IN ({})'.format(temp_str)
    type_code_str = 'TRUE'
    if self._type_code:
      temp_str = ','.join(['"{}"'.format(v) for v in self._type_code])
      type_code_str = 'encTypeCode IN ({})'.format(temp_str)
    type_sys_str = 'encTypeSystem="{}"'.format(
        self._type_system) if self._type_system else 'TRUE'
    return '{} AND {} AND {}'.format(loc_str, type_code_str, type_sys_str)


# TODO add Patient filtering criteria to this query API.
class PatientQuery():
  """The main class for specifying a patient query.

  The expected usage flow is:
  - The user specifies where the data comes from and what query engine should
    be used, e.g., Parquet files with Spark, a SQL engine like BigQuery, or even
    a FHIR server/API (future).
  - Constraints are set, e.g., observation codes, values, date, etc.
  - The query is run on the underlying engine and a Pandas DataFrame is created.
  - The DataFrame is fetched or more manipulation is done on it by the library.
  """

  def __init__(self, code_system: str = None):
    self._code_constraint = {}
    self._enc_constraint = _EncounterContraints()
    self._include_all_codes = False
    self._all_codes_min_time = None
    self._all_codes_max_time = None
    self._code_system = code_system

  def include_obs_in_value_and_time_range(self, code: str,
      min_val: float = None, max_val: float = None, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    if code in self._code_constraint:
      raise ValueError('Duplicate constraints for code {}'.format(code))
    self._code_constraint[code] = _ObsConstraints(
        code, value_sys=self._code_system, min_value=min_val,
        max_value=max_val, min_time=min_time, max_time=max_time)
    return self

  def include_obs_values_in_time_range(self, code: str,
      values: List[str] = None, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    if code in self._code_constraint:
      raise ValueError('Duplicate constraints for code {}'.format(code))
    self._code_constraint[code] = _ObsConstraints(
        code, values=values, value_sys=self._code_system, min_time=min_time,
        max_time=max_time)
    return self

  def include_all_other_codes(self, include: bool = True, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    self._include_all_codes = include
    self._all_codes_min_time = min_time
    self._all_codes_max_time = max_time
    return self

  def encounter_constraints(self, locationId: List[str] = None,
      typeSystem: str = None, typeCode: List[str] = None):
    """Specifies constraints on encounters to be included.

    Note calling this erases previous encounter constraints. Any constraint
    that is None is ignored.

    Args:
      locationId: The list of locations that should be kept or None if there are
        no location constraints.
      typeSystem: An string representing the type system or None.
      typeCode: A list of encounter type codes that should be kept or None if
        there are no type constraints.
    """
    self._enc_constraint = _EncounterContraints(
        locationId, typeSystem, typeCode)

  def _all_obs_constraints(self) -> str:
    if not self._code_constraint:
      if self._include_all_codes:
        return 'TRUE'
      else:
        return 'FALSE'
    constraints_str = ' OR '.join(
        [self._code_constraint[code].sql() for code in self._code_constraint])
    if not self._include_all_codes:
      return '({})'.format(constraints_str)
    others_str = ' AND '.join(
        ['coding.code!="{}"'.format(code) for code in self._code_constraint] + [
            _ObsConstraints.time_constraint(self._all_codes_min_time,
                                            self._all_codes_max_time)])
    return '({} OR ({}))'.format(constraints_str, others_str)

  def all_constraints_sql(self) -> str:
    obs_str = self._all_obs_constraints()
    enc_str = '{}'.format(
        self._enc_constraint.sql()) if self._enc_constraint else 'TRUE'
    return '{} AND {}'.format(obs_str, enc_str)

  # TODO remove `base_url` parameter once issue #55 is fixed.
  def get_patient_obs_view(self, base_url: str) -> pandas.DataFrame:
    """Creates a patient * observation code aggregated view.

    For each patient and observation code, group all such observation and
    returns some aggregated values. Loads the data if that is necessary.

    Args:
      base_url: See issue #55!

    Returns:
      A Pandas DataFrame with the following columns:
        - `patientId` the patient for whom the aggregation is done
        - `birthDate` the patient's birth date
        - `gender` the patient's gender
        - `code` the code of the observation in the `code_system`
        - `num_obs` number of observations with above spec
        - `min_value` the minimum obs value in the specified period or `None` if
          this observation does not have a numeric value.
        - `max_value` the maximum obs value in the specified period or `None`
        - `min_date` the first time that an observation with the given code was
           observed in the specified period.
        - `max_date` ditto for last time
        - `first_value` the value corresponding to `min_date`
        - `last_value` the value corresponding to `max_date`
        - `first_value_code` the coded value corresponding to `min_date`
        - `last_value_code` the coded value corresponding to `max_date`
    """
    raise NotImplementedError('This should be implemented by sub-classes!')

  def get_patient_encounter_view(self, base_url: str,
      force_location_type_columns: bool = True) -> pandas.DataFrame:
    """Aggregates encounters for each patient based on location, type, etc.

    For each patient and encounter attributes (e.g., location, type, etc.) finds
    aggregate values. Loads the data if that is necessary.

    Args:
      base_url: See issue #55!
      force_location_type_columns: whehter to include location and type related
        columns regardless of the constraints. Note this can duplicate a single
        encounter to many rows if that row has multiple locations and types.

    Returns:
      A Pandas DataFrame with the following columns:
        - `patientId` the patient for whom the aggregation is done
        - `locationId` the location ID of where the encounters took place; this
          and the next one are provided only if there is a location constraint
          or `force_location_type_columns` is `True`.
        - `locationDisplay` the human readable name of the location
        - `encTypeSystem` the encounter type system this and the next one are
          provided only if there is a type constraint or
          `force_location_type_columns` is `True`.
        - `encTypeCode` the encounter type code
        - `numEncounters` number of encounters with that type and location
        - `firstDate` the first date such an encounter happened
        - `lastDate` the last date such an encounter happened
    """
    raise NotImplementedError('This should be implemented by sub-classes!')


class _SparkPatientQuery(PatientQuery):

  def __init__(self, file_root: str, code_system: str):
    super().__init__(code_system)
    self._file_root = file_root
    self._spark = None
    self._patient_df = None
    self._obs_df = None
    self._flat_obs = None
    self._patient_agg_obs_df = None
    self._enc_df = None

  def _make_sure_spark(self):
    if not self._spark:
      # TODO add the option for using a running Spark cluster.
      conf = (SparkConf()
              .setMaster('local[10]')
              .setAppName('IndicatorsApp')
              .set('spark.driver.memory', '10g')
              .set('spark.executor.memory', '4g')
              # See: https://spark.apache.org/docs/latest/security.html
              .set('spark.authenticate', 'true')
              )
      self._spark = SparkSession.builder.config(conf=conf).getOrCreate()

  def _make_sure_patient(self):
    if not self._patient_df:
      # Loading Parquet files and flattening only happens once.
      self._patient_df = self._spark.read.parquet(self._file_root + '/Patient')
      # TODO create inspection functions
      common.custom_log(
          'Number of Patient resources= {}'.format(self._patient_df.count()))

  def _make_sure_obs(self):
    if not self._obs_df:
      self._obs_df = self._spark.read.parquet(self._file_root + '/Observation')
      common.custom_log(
          'Number of Observation resources= {}'.format(self._obs_df.count()))
    if not self._flat_obs:
      self._flat_obs = _SparkPatientQuery._flatten_obs(
          self._obs_df, self._code_system)
      common.custom_log(
          'Number of flattened obs rows = {}'.format(self._flat_obs.count()))

  def _make_sure_encounter(self):
    if not self._enc_df:
      self._enc_df = self._spark.read.parquet(self._file_root + '/Encounter')
      common.custom_log(
          'Number of Encounter resources= {}'.format(self._enc_df.count()))

  def get_patient_obs_view(self, base_url: str) -> pandas.DataFrame:
    """See super-class doc."""
    self._make_sure_spark()
    self._make_sure_patient()
    self._make_sure_obs()
    self._make_sure_encounter()
    base_patient_url = base_url + 'Patient/'
    # Recalculating the rest is needed since the constraints can be updated.
    flat_enc = self._flatten_encounter(base_url + 'Encounter/',
                                       force_location_type_columns=False)
    # TODO figure where `context` comes from and why.
    join_df = self._flat_obs.join(
        flat_enc, flat_enc.encounterId == self._flat_obs.encounterId).where(
        self.all_constraints_sql())
    agg_obs_df = _SparkPatientQuery._aggregate_patient_codes(join_df)
    common.custom_log(
      'Number of aggregated obs= {}'.format(agg_obs_df.count()))
    self._patient_agg_obs_df = _SparkPatientQuery._join_patients_agg_obs(
        self._patient_df, agg_obs_df, base_patient_url)
    common.custom_log('Number of joined patient_agg_obs= {}'.format(
        self._patient_agg_obs_df.count()))
    # Spark is supposed to automatically cache DFs after shuffle but it seems
    # this is not happening!
    self._patient_agg_obs_df.cache()
    temp_pd_df = self._patient_agg_obs_df.toPandas()
    common.custom_log('patient_obs_view size= {}'.format(temp_pd_df.index.size))
    temp_pd_df['last_value'] = temp_pd_df.max_date_value.str.split(
        DATE_VALUE_SEPARATOR, expand=True)[1]
    temp_pd_df['first_value'] = temp_pd_df.min_date_value.str.split(
        DATE_VALUE_SEPARATOR, expand=True)[1]
    temp_pd_df['last_value_code'] = temp_pd_df.max_date_value_code.str.split(
        DATE_VALUE_SEPARATOR, expand=True)[1]
    temp_pd_df['first_value_code'] = temp_pd_df.min_date_value_code.str.split(
        DATE_VALUE_SEPARATOR, expand=True)[1]
    # This is good for debug!
    # return temp_pd_df
    return temp_pd_df[[
        'patientId', 'birthDate', 'gender', 'code', 'num_obs', 'min_value',
        'max_value', 'min_date', 'max_date', 'first_value', 'last_value',
        'first_value_code', 'last_value_code']]

  def get_patient_encounter_view(self, base_url: str,
      force_location_type_columns: bool = True) -> pandas.DataFrame:
    """See super-class doc."""
    self._make_sure_spark()
    self._make_sure_patient()
    self._make_sure_encounter()
    flat_enc = self._flatten_encounter(base_url + 'Encounter/',
                                       force_location_type_columns)
    column_list = ['encPatientId']
    if self._enc_constraint.has_location() or force_location_type_columns:
      column_list += ['locationId', 'locationDisplay']
    if self._enc_constraint.has_type() or force_location_type_columns:
      column_list += ['encTypeSystem', 'encTypeCode']
    return flat_enc.groupBy(column_list).agg(
        F.count('*').alias('num_encounters'),
        F.min('first').alias('firstDate'),
        F.max('last').alias('lastDate')
    ).toPandas()

  def _flatten_encounter(self, base_encounter_url: str,
      force_location_type_columns: bool = True):
    """Returns a custom flat view of encoutners."""
    # When merging flattened encounters and observations, we need to be careful
    # with flattened columns for encounter type and location and only include
    # them if there is a constraints on them. Otherwise we may end up with a
    # single observation repeated multiple times in the view.
    flat_df = self._enc_df.select(
        'subject', 'id', 'location', 'type', 'period').withColumn(
        'encounterId', F.regexp_replace('id', base_encounter_url, ''))
    column_list = [
        F.col('encounterId'),
        F.col('subject.patientId').alias('encPatientId'),
        F.col('period.start').alias('first'),
        F.col('period.end').alias('last')]
    if self._enc_constraint.has_location() or force_location_type_columns:
      flat_df = flat_df.withColumn('locationFlat', F.explode_outer('location'))
      column_list += [
          F.col('locationFlat.location.LocationId').alias('locationId'),
          F.col('locationFlat.location.display').alias('locationDisplay')]
    if self._enc_constraint.has_type() or force_location_type_columns:
      flat_df = flat_df.withColumn('typeFlat', F.explode_outer('type'))
      column_list += [
          F.col('typeFlat.coding.system').alias('encTypeSystem'),
          F.col('typeFlat.coding.code').alias('encTypeCode')]
    return flat_df.select(column_list).where(self._enc_constraint.sql())

  @staticmethod
  def _flatten_obs(obs: DataFrame, code_system: str = None) -> DataFrame:
    """Creates a flat version of Observation FHIR resources.

    Note `code_system` is only applied on `code.coding` which is a required
    filed, i.e., it is not applied on `value.codeableConcept.coding`.

    Args:
      obs: A collection of Observation FHIR resources.
      code_system: The code system to be used for filtering `code.coding`.
    Returns:
      A DataFrame with the following columns (note one input observation might
      be repeated, once for each of its codes):
      - `coding` from the input obsservation's `code.coding`
      - `valueCoding` from the input's `value.codeableConcept.coding`
      - `value` from the input's `value`
      - `patientId` from the input's `subject.patientId`
      - `dateTime` from the input's `effective.dateTime`
    """
    sys_str = 'coding.system="{}"'.format(
        code_system) if code_system else 'coding.system IS NULL'
    value_sys_str_base = 'valueCoding.system="{}"'.format(
        code_system) if code_system else 'valueCoding.system IS NULL'
    value_sys_str = '(valueCoding IS NULL OR {})'.format(value_sys_str_base)
    merge_udf = F.UserDefinedFunction(
        lambda d, v: merge_date_and_value(d, v), T.StringType())
    return obs.withColumn('coding', F.explode('code.coding')).where(
        sys_str).withColumn('valueCoding', # Note valueCoding can be null.
        F.explode_outer('value.codeableConcept.coding')).where(
        value_sys_str).withColumn('dateAndValue', merge_udf(
        F.col('effective.dateTime'), F.col('value.quantity.value'))).withColumn(
        'dateAndValueCode', merge_udf(F.col('effective.dateTime'),
                                      F.col('valueCoding.code'))).select(
        F.col('coding'),
        F.col('valueCoding'),
        F.col('value'),
        F.col('subject.patientId').alias('patientId'),
        F.col('effective.dateTime').alias('dateTime'),
        F.col('dateAndValue'),
        F.col('dateAndValueCode'),
        F.col('context.EncounterId').alias('encounterId')
    )

  @staticmethod
  def _aggregate_patient_codes(flat_obs: DataFrame) -> DataFrame:
    """ Find aggregates for each patientId, conceptCode, and codedValue.

    Args:
        flat_obs: A collection of flattened Observations.
    Returns:
      A DataFrame with the following columns:
    """
    return flat_obs.groupBy(['patientId', 'coding']).agg(
        F.count('*').alias('num_obs'),
        F.min('value.quantity.value').alias('min_value'),
        F.max('value.quantity.value').alias('max_value'),
        F.min('dateTime').alias('min_date'),
        F.max('dateTime').alias('max_date'),
        F.min('dateAndValue').alias('min_date_value'),
        F.max('dateAndValue').alias('max_date_value'),
        F.min('dateAndValueCode').alias('min_date_value_code'),
        F.max('dateAndValueCode').alias('max_date_value_code'),
    )

  @staticmethod
  def _join_patients_agg_obs(
      patients: DataFrame,
      agg_obs: DataFrame,
      base_patient_url: str) -> DataFrame:
    """Joins a collection of Patient FHIR resources with an aggregated obs set.

    Args:
      patients: A collection of Patient FHIR resources.
      agg_obs: Aggregated observations from `aggregate_all_codes_per_patient()`.
    Returns:
      Same `agg_obs` with corresponding patient information joined.
    """
    flat_patients = patients.select(
        patients.id, patients.birthDate, patients.gender).withColumn(
        'actual_id', F.regexp_replace('id', base_patient_url, '')).select(
        'actual_id', 'birthDate', 'gender')
    return flat_patients.join(
        agg_obs, flat_patients.actual_id == agg_obs.patientId).select(
        'patientId', 'birthDate', 'gender', 'coding.code',
        'num_obs', 'min_value', 'max_value', 'min_date', 'max_date',
        'min_date_value', 'max_date_value', 'min_date_value_code',
        'max_date_value_code')


class _BigQueryEncounterConstraints:
  '''
  Encounter constraints helper class that will be set up for querying BigQuery
  '''
  def __init__(self, location_ids: tp.Optional[tp.Iterable[str]] = None,
      type_system: tp.Optional[str] = None, type_codes: tp.Optional[tp.Iterable[str]] = None):
    self.location_ids = location_ids
    self.type_system = type_system
    self.type_codes = type_codes

  def has_location(self) -> bool:
    return self._location_id != None

  def has_type(self) -> bool:
    return (self._type_code != None) or (self._type_system != None)

  def sql(self) -> str:
    """This creates a constraint string with WHERE syntax in SQL."""
    loc_str = 'TRUE'
    if self._location_id:
      temp_str = ','.join(['"{}"'.format(v) for v in self._location_id])
      loc_str = 'locationId IN ({})'.format(temp_str)
    type_code_str = 'TRUE'
    if self._type_code:
      temp_str = ','.join(['"{}"'.format(v) for v in self._type_code])
      type_code_str = 'encTypeCode IN ({})'.format(temp_str)
    type_sys_str = 'encTypeSystem="{}"'.format(
        self._type_system) if self._type_system else 'TRUE'
    return '{} AND {} AND {}'.format(loc_str, type_code_str, type_sys_str)


class _BigQueryObsConstraints:
  '''
  Observation constraints helper class for querying Big Query
  '''

class _BigQueryPatientQuery(PatientQuery):
  '''
  Concrete implementation of PatientQuery class that serves data stored in BigQuery
  '''

  def __init__(self, bq_dataset: str, code_system: str):
    super().__init__(code_system)

    self._bq_dataset = bq_dataset

    #TODO(gdevanla): All the below statements can likely go to base class
    self._code_constraint = {}
    self._enc_constraint = _BigQueryEncounterConstraints()
    self._include_all_codes = False
    self._all_codes_min_time = None
    self._all_codes_max_time = None
    self._code_system = code_system

  #TODO(gdevanla): This can be moved to base class just by injecting ObservationConstraints class
  def include_obs_in_value_and_time_range(self, code: str,
      min_val: float = None, max_val: float = None, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    if code in self._code_constraint:
      raise ValueError('Duplicate constraints for code {}'.format(code))
    self._code_constraint[code] = _BigQueryObsConstraints(
        code, value_sys=self._code_system, min_value=min_val,
        max_value=max_val, min_time=min_time, max_time=max_time)
    return self

  #TODO(gdevanla): This can be moved to base class just by injecting ObservationConstraints class
  def include_obs_values_in_time_range(self, code: str,
      values: List[str] = None, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    if code in self._code_constraint:
      raise ValueError('Duplicate constraints for code {}'.format(code))
    self._code_constraint[code] = _BigQueryObsConstraints(
        code, values=values, value_sys=self._code_system, min_time=min_time,
        max_time=max_time)
    return self

  #TODO(gdevanla): This can be moved to base class
  def include_all_other_codes(self, include: bool = True, min_time: str = None,
      max_time: str = None) -> PatientQuery:
    self._include_all_codes = include
    self._all_codes_min_time = min_time
    self._all_codes_max_time = max_time
    return self

  #TODO(gdevanla): This can be moved to base class just by injecting EncounterConstraints class
  def encounter_constraints(self,
                            location_ids: tp.Optional[tp.Iterable[str]] = None,
      type_system: tp.Optional[str] = None,
                            type_codes: tp.Optional[tp.Iterable[str]] = None):
    """Specifies constraints on encounters to be included.

    Note calling this erases previous encounter constraints. Any constraint
    that is None is ignored.

    Args:
      locationId: The list of locations that should be kept or None if there are
        no location constraints.
      typeSystem: An string representing the type system or None.
      typeCode: A list of encounter type codes that should be kept or None if
        there are no type constraints.
    """
    self._enc_constraint = _BigQueryEncounterConstraints(
      location_ids=location_ids,
      type_system=type_system,
      type_codes=type_codes)


  @classmethod
  def _build_encounter_query(cls, *, bq_dataset: str,
                             base_url: str, table_name: str,
                             location_ids: tp.Optional[tp.Iterable[str]],
                             type_system: tp.Optional[str] = None,
                             type_codes: tp.Optional[tp.Iterable[str]] = None,
                             force_location_type_columns: bool = True):
    '''
    Helper function to build the sql query which will only query the Encounter table
    Sample Query:
        WITH S AS (
        select * from `learnbq-345320.fhir_sample.encounter`
        )
        select S.id as encounterId,
        S.subject.PatientId as encPatientId,
        S.period.start as first,
        S.period.end as last,
        C.system, C.code,
        L.location.LocationId, L.location.display
        from S, unnest(s.type) as T, unnest(T.coding) as C left join unnest(s.location) as L
        where C.system = 'system3000' and C.code = 'code3000'
        and L.location.locationId in ('test')
    '''

    sql_template = '''
    WITH S AS (
          select * from {data_set}.{table_name}
          )
          select replace(S.id, '{base_url}', '') as encounterId,
          S.subject.PatientId as encPatientId,
          S.period.start as first,
          S.period.end as last,
          C.system, C.code,
          L.location.LocationId, L.location.display
          from S, unnest(s.type) as T, unnest(T.coding) as C left join unnest(s.location) as L
          --C.system = 'system3000' and C.code = 'code3000'
          --and L.location.locationId in ('test')
    '''.format(table_name=table_name, base_url=base_url, data_set=bq_dataset)

    clause_location_id = None
    if location_ids:
      clause_location_id = 'L.location.locationId in ({})'.format(_build_in_list_with_quotes(location_ids))
    clause_type_system = None
    if type_system:
      clause_type_system = "C.system = \'{}\'".format(type_system)
    clause_type_codes = None
    if type_codes:
      clause_type_codes = 'C.code in ({})'.format(_build_in_list_with_quotes(type_codes))

    where_clause = " and ".join(x for x in [clause_location_id, clause_type_system, clause_type_codes]
                                if x)
    if where_clause:
      return sql_template + " where " + where_clause
    return sql_template


  def get_patient_encounter_view(self, base_url: str,
      force_location_type_columns: bool = True) -> pandas.DataFrame:

    sql = self._build_encounter_query(
      bq_dataset=self._bq_dataset,
      table_name='encounter',
      base_url=base_url,
      location_ids=self._enc_constraint.location_ids if self._enc_constraint else None,
      type_system=self._enc_constraint.type_system if self._enc_constraint else None,
      type_codes=self._enc_constraint.type_codes if self._enc_constraint else None,
      force_location_type_columns=force_location_type_columns
    )

    client = bigquery.Client()
    patient_enc = client.query(sql).to_dataframe()
    return patient_enc
