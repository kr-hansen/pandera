"""Pandas Parsing, Validation, and Error Reporting Backends."""

import copy
import itertools
from typing import Any, List, Optional

import pandas as pd

from pandera.backends.pandas.base import ColumnInfo, PandasSchemaBackend
from pandera.error_formatters import reshape_failure_cases, scalar_failure_case
from pandera.error_handlers import SchemaErrorHandler
from pandera.errors import ParserError, SchemaError, SchemaErrors

# TODO: rename backends to match the core.pandas module names, e.g.:
# - PandasSchemaContainerBackend -> DataFrameSchemaBackend
# - PandasSchemaFieldBackend -> ArraySchemaBackend
# - PandasSchemaFieldComponentBackend -> ColumnSchemaBackend
#
# Note that SeriesSchema will just use the ArraySchemaBackend


class PandasSchemaContainerBackend(PandasSchemaBackend[pd.DataFrame]):
    def __init__(self):
        from pandera.backends.pandas.component import (
            PandasSchemaFieldComponentBackend,
        )

        self.field_backend = PandasSchemaFieldComponentBackend()

    def preprocess(
        self, check_obj: pd.DataFrame, name: str = None, inplace: bool = False
    ):
        if not inplace:
            check_obj = check_obj.copy()
        return check_obj

    def validate(
        self,
        check_obj: pd.DataFrame,
        schema,
        *,
        head: Optional[int] = None,
        tail: Optional[int] = None,
        sample: Optional[int] = None,
        random_state: Optional[int] = None,
        lazy: bool = False,
        inplace: bool = False,
    ):
        error_handler = SchemaErrorHandler(lazy)

        check_obj = self.preprocess(check_obj, inplace=inplace)
        if hasattr(check_obj, "pandera"):
            check_obj = check_obj.pandera.add_schema(schema)

        column_info = self.collect_column_info(check_obj, schema)

        # check the container metadata, e.g. field names
        self.check_column_names_are_unique(check_obj, schema)
        self.check_column_presence(check_obj, schema, column_info)

        # try to coerce datatypes
        check_obj = self.coerce_dtype(check_obj, schema, error_handler)

        # collect schema components and prepare check object to be validated
        schema_components = self.collect_schema_components(
            check_obj, schema, column_info
        )
        check_obj_subsample = self.subsample(
            check_obj, head, tail, sample, random_state
        )
        self.run_schema_component_checks(
            check_obj_subsample, schema_components, lazy, error_handler
        )
        self.run_checks(check_obj_subsample, schema, error_handler)
        self.check_column_values_are_unique(check_obj_subsample, schema)

        return check_obj

    def run_schema_component_checks(
        self,
        check_obj: pd.DataFrame,
        schema_components: List,
        lazy: bool,
        error_handler: SchemaErrorHandler,
    ):
        check_results = []
        # schema-component-level checks
        for schema_component in schema_components:
            try:
                result = self.field_backend.validate(
                    check_obj, schema_component, lazy=lazy, inplace=True
                )
                check_results.append(isinstance(result, pd.DataFrame))
            except SchemaError as err:
                error_handler.collect_error("schema_component_check", err)
            except SchemaErrors as err:
                for schema_error_dict in err.schema_errors:
                    error_handler.collect_error(
                        "schema_component_check", schema_error_dict["error"]
                    )
        assert all(check_results)

    def run_checks(self, check_obj: pd.DataFrame, schema, error_handler):
        # dataframe-level checks
        check_results = []
        for check_index, check in enumerate(schema.checks):
            try:
                check_results.append(
                    self.run_check(check_obj, schema, check, check_index)
                )
            except SchemaError as err:
                error_handler.collect_error("dataframe_check", err)

    def collect_column_info(
        self, check_obj: pd.DataFrame, schema
    ) -> ColumnInfo:
        column_names: List[Any] = []
        absent_column_names: List[Any] = []
        lazy_exclude_column_names: List[Any] = []

        for col_name, col_schema in schema.columns.items():
            if (
                not col_schema.regex
                and col_name not in check_obj
                and col_schema.required
            ):
                absent_column_names.append(col_name)
                if schema.lazy:
                    # TODO: remove this since we can just use
                    # absent_column_names in the collect_schema_components
                    # method
                    lazy_exclude_column_names.append(col_name)

            if col_schema.regex:
                try:
                    column_names.extend(
                        col_schema.get_regex_columns(check_obj.columns)
                    )
                except SchemaError:
                    pass
            elif col_name in check_obj.columns:
                column_names.append(col_name)

        # drop adjacent duplicated column names
        destuttered_column_names = [*check_obj.columns]
        if check_obj.columns.has_duplicates:
            destuttered_column_names = [
                k for k, _ in itertools.groupby(check_obj.columns)
            ]

        return ColumnInfo(
            sorted_column_names=dict.fromkeys(column_names),
            expanded_column_names=frozenset(column_names),
            destuttered_column_names=destuttered_column_names,
            absent_column_names=absent_column_names,
            lazy_exclude_column_names=lazy_exclude_column_names,
        )

    def collect_schema_components(
        self,
        check_obj: pd.DataFrame,
        schema,
        column_info: ColumnInfo,
    ):
        schema_components = []
        for col_name, col in schema.columns.items():
            if (
                col.required or col_name in check_obj
            ) and col_name not in column_info.lazy_exclude_column_names:
                if schema.dtype is not None:
                    # override column dtype with dataframe dtype
                    col = copy.deepcopy(col)
                    col.dtype = schema.dtype
                schema_components.append(col)

        if schema.index is not None:
            schema_components.append(schema.index)
        return schema_components

    ###########
    # Parsers #
    ###########

    def strict_filter_columns(
        self, check_obj: pd.DataFrame, schema, column_info: ColumnInfo
    ):
        # dataframe strictness check makes sure all columns in the dataframe
        # are specified in the dataframe schema
        if not (schema.strict or schema.ordered):
            return

        filter_out_columns = []

        for column in column_info.destuttered_column_names:
            is_schema_col = column in column_info.expanded_column_names
            if schema.strict and not is_schema_col:
                raise SchemaError(
                    schema=schema,
                    data=check_obj,
                    message=(
                        f"column '{column}' not in {schema.__class__.__name__}"
                        f" {schema.columns}"
                    ),
                    failure_cases=scalar_failure_case(column),
                    check="column_in_schema",
                    reason_code="column_not_in_schema",
                )
            if schema.strict == "filter" and not is_schema_col:
                filter_out_columns.append(column)
            if schema.ordered and is_schema_col:
                try:
                    next_ordered_col = next(column_info.sorted_column_names)
                except StopIteration:
                    pass
                if next_ordered_col != column:
                    SchemaError(
                        schema=schema,
                        data=check_obj,
                        message=f"column '{column}' out-of-order",
                        failure_cases=scalar_failure_case(column),
                        check="column_ordered",
                        reason_code="column_not_ordered",
                    )

        if schema.strict == "filter":
            check_obj.drop(labels=filter_out_columns, inplace=True, axis=1)

        return check_obj

    def coerce_dtype(
        self,
        check_obj: pd.DataFrame,
        schema,
        error_handler: SchemaErrorHandler,
    ):
        if not (
            schema.coerce
            or (schema.index is not None and schema.index.coerce)
            or any(col.coerce for col in schema.columns.values())
        ):
            return check_obj

        try:
            check_obj = self._coerce_dtype(check_obj, schema)
        except SchemaErrors as err:
            for schema_error_dict in err.schema_errors:
                if not schema.lazy:
                    # raise the first error immediately if not doing lazy
                    # validation
                    raise schema_error_dict["error"]
                error_handler.collect_error(
                    "schema_component_check", schema_error_dict["error"]
                )
        except SchemaError as err:
            if not schema.lazy:
                raise err
            error_handler.collect_error("schema_component_check", err)
        return check_obj

    def _coerce_dtype(self, obj: pd.DataFrame, schema) -> pd.DataFrame:
        """Coerce dataframe to the type specified in dtype.

        :param obj: dataframe to coerce.
        :returns: dataframe with coerced dtypes
        """
        # TODO: clean up the error handling!
        error_handler = SchemaErrorHandler(lazy=True)

        def _coerce_df_dtype(obj: pd.DataFrame, schema) -> pd.DataFrame:
            if schema.dtype is None:
                raise ValueError(
                    "dtype argument is None. Must specify this argument "
                    "to coerce dtype"
                )

            try:
                return schema.dtype.try_coerce(obj)
            except ParserError as exc:
                raise SchemaError(
                    schema=schema,
                    data=obj,
                    message=(
                        f"Error while coercing '{schema.name}' to type "
                        f"{schema.dtype}: {exc}\n{exc.failure_cases}"
                    ),
                    failure_cases=exc.failure_cases,
                    check=f"coerce_dtype('{schema.dtype}')",
                ) from exc

        def _try_coercion(coerce_fn, obj):
            try:
                return coerce_fn(obj)
            except SchemaError as exc:
                error_handler.collect_error("dtype_coercion_error", exc)
                return obj

        for colname, col_schema in schema.columns.items():
            if col_schema.regex:
                try:
                    matched_columns = col_schema.get_regex_columns(obj.columns)
                except SchemaError:
                    matched_columns = pd.Index([])

                for matched_colname in matched_columns:
                    if col_schema.coerce or schema.coerce:
                        obj[matched_colname] = _try_coercion(
                            col_schema.coerce_dtype, obj[matched_colname]
                        )
            elif (
                (col_schema.coerce or schema.coerce)
                and schema.dtype is None
                and colname in obj
            ):
                obj[colname] = _try_coercion(
                    col_schema.coerce_dtype, obj[colname]
                )

        if schema.dtype is not None:
            obj = _try_coercion(_coerce_df_dtype, obj)
        if schema.index is not None and (schema.index.coerce or schema.coerce):
            index_schema = copy.deepcopy(schema.index)
            if schema.coerce:
                # coercing at the dataframe-level should apply index coercion
                # for both single- and multi-indexes.
                index_schema._coerce = True
            coerced_index = _try_coercion(index_schema.coerce_dtype, obj.index)
            if coerced_index is not None:
                obj.index = coerced_index

        if error_handler.collected_errors:
            raise SchemaErrors(schema, error_handler.collected_errors, obj)

        return obj

    ##########
    # Checks #
    ##########

    def check_column_names_are_unique(self, check_obj: pd.DataFrame, schema):
        if not schema.unique_column_names:
            return
        failed = check_obj.columns[check_obj.columns.duplicated()]
        if failed.any():
            SchemaError(
                schema=schema,
                data=check_obj,
                message=(
                    "dataframe contains multiple columns with label(s): "
                    f"{failed.tolist()}"
                ),
                failure_cases=scalar_failure_case(failed),
                check="dataframe_column_labels_unique",
                reason_code="duplicate_dataframe_column_labels",
            )

    def check_column_presence(
        self, check_obj: pd.DataFrame, schema, column_info: ColumnInfo
    ):
        if column_info.absent_column_names:
            # TODO: only report the first absent column for now, need to update
            # this when backend stuff is complete
            colname, *_ = column_info.absent_column_names
            SchemaError(
                schema=schema,
                data=check_obj,
                message=(
                    f"column '{colname}' not in dataframe\n{check_obj.head()}"
                ),
                failure_cases=scalar_failure_case(colname),
                check="column_in_dataframe",
                reason_code="column_not_in_dataframe",
            )

    def check_column_values_are_unique(self, check_obj: pd.DataFrame, schema):
        if not schema.unique:
            return

        # NOTE: fix this pylint error
        # pylint: disable=not-an-iterable
        temp_unique: List[List] = (
            [schema.unique]
            if all(isinstance(x, str) for x in schema.unique)
            else schema.unique
        )
        for lst in temp_unique:
            duplicates = check_obj.duplicated(subset=lst, keep=False)
            if duplicates.any():
                # NOTE: this is a hack to support pyspark.pandas, need to
                # figure out a workaround to error: "Cannot combine the
                # series or dataframe because it comes from a different
                # dataframe."
                if type(duplicates).__module__.startswith("pyspark.pandas"):
                    # pylint: disable=import-outside-toplevel
                    import pyspark.pandas as ps

                    with ps.option_context("compute.ops_on_diff_frames", True):
                        failure_cases = check_obj.loc[duplicates, lst]
                else:
                    failure_cases = check_obj.loc[duplicates, lst]

                failure_cases = reshape_failure_cases(failure_cases)
                SchemaError(
                    schema=schema,
                    data=check_obj,
                    message=f"columns '{*lst,}' not unique:\n{failure_cases}",
                    failure_cases=failure_cases,
                    check="multiple_fields_uniqueness",
                    reason_code="duplicates",
                )
