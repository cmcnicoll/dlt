import os
from typing import Callable, List, Dict, Sequence, Tuple, Set, Optional
from concurrent.futures import Future, Executor

from dlt.common import pendulum, json, logger, sleep
from dlt.common.configuration import with_config, known_sections
from dlt.common.configuration.accessors import config
from dlt.common.configuration.container import Container
from dlt.common.destination import TLoaderFileFormat
from dlt.common.runners import TRunMetrics, Runnable, NullExecutor
from dlt.common.runtime import signals
from dlt.common.runtime.collector import Collector, NULL_COLLECTOR
from dlt.common.schema.typing import TStoredSchema
from dlt.common.schema.utils import merge_schema_updates
from dlt.common.storages.exceptions import SchemaNotFoundError
from dlt.common.storages import NormalizeStorage, SchemaStorage, LoadStorage, LoadStorageConfiguration, NormalizeStorageConfiguration
from dlt.common.schema import TSchemaUpdate, Schema
from dlt.common.schema.exceptions import CannotCoerceColumnException
from dlt.common.pipeline import NormalizeInfo
from dlt.common.utils import chunks, TRowCount, merge_row_count, increase_row_count

from dlt.normalize.configuration import NormalizeConfiguration
from dlt.normalize.items_normalizers import ParquetItemsNormalizer, JsonLItemsNormalizer, ItemsNormalizer

# normalize worker wrapping function (map_parallel, map_single) return type
TMapFuncRV = Tuple[Sequence[TSchemaUpdate], TRowCount]
# normalize worker wrapping function signature
TMapFuncType = Callable[[Schema, str, Sequence[str]], TMapFuncRV]  # input parameters: (schema name, load_id, list of files to process)
# tuple returned by the worker
TWorkerRV = Tuple[List[TSchemaUpdate], int, List[str], TRowCount]


class Normalize(Runnable[Executor]):
    pool: Executor
    @with_config(spec=NormalizeConfiguration, sections=(known_sections.NORMALIZE,))
    def __init__(self, collector: Collector = NULL_COLLECTOR, schema_storage: SchemaStorage = None, config: NormalizeConfiguration = config.value) -> None:
        self.config = config
        self.collector = collector
        self.normalize_storage: NormalizeStorage = None
        self.pool = NullExecutor()
        self.load_storage: LoadStorage = None
        self.schema_storage: SchemaStorage = None
        self._row_counts: TRowCount = {}

        # setup storages
        self.create_storages()
        # create schema storage with give type
        self.schema_storage = schema_storage or SchemaStorage(self.config._schema_storage_config, makedirs=True)

    def create_storages(self) -> None:
        # pass initial normalize storage config embedded in normalize config
        self.normalize_storage = NormalizeStorage(True, config=self.config._normalize_storage_config)
        # normalize saves in preferred format but can read all supported formats
        self.load_storage = LoadStorage(
            True,
            self.config.destination_capabilities.preferred_loader_file_format,
            LoadStorage.ALL_SUPPORTED_FILE_FORMATS,
            config=self.config._load_storage_config
        )

    @staticmethod
    def load_or_create_schema(schema_storage: SchemaStorage, schema_name: str) -> Schema:
        try:
            schema = schema_storage.load_schema(schema_name)
            schema.update_normalizers()
            logger.info(f"Loaded schema with name {schema_name} with version {schema.stored_version}")
        except SchemaNotFoundError:
            schema = Schema(schema_name)
            logger.info(f"Created new schema with name {schema_name}")
        return schema

    @staticmethod
    def w_normalize_files(
        config: NormalizeConfiguration,
        normalize_storage_config: NormalizeStorageConfiguration,
        loader_storage_config: LoadStorageConfiguration,
        stored_schema: TStoredSchema,
        load_id: str,
        extracted_items_files: Sequence[str],
    ) -> TWorkerRV:
        destination_caps = config.destination_capabilities
        schema_updates: List[TSchemaUpdate] = []
        total_items = 0
        row_counts: TRowCount = {}
        load_storages: Dict[TLoaderFileFormat, LoadStorage] = {}

        def _get_load_storage(file_format: TLoaderFileFormat) -> LoadStorage:
            # TODO: capabilities.supported_*_formats can be None, it should have defaults
            supported_formats = destination_caps.supported_loader_file_formats or []
            if file_format == "parquet":
                if file_format in supported_formats:
                    supported_formats.append("arrow")  # TODO: Hack to make load storage use the correct writer
                    file_format = "arrow"
                else:
                    # Use default storage if parquet is not supported to make normalizer fallback to read rows from the file
                    file_format = destination_caps.preferred_loader_file_format or destination_caps.preferred_staging_file_format
            else:
                file_format = destination_caps.preferred_loader_file_format or destination_caps.preferred_staging_file_format
            if storage := load_storages.get(file_format):
                return storage
            storage = load_storages[file_format] = LoadStorage(False, file_format, supported_formats, loader_storage_config)
            return storage

        # process all files with data items and write to buffered item storage
        with Container().injectable_context(destination_caps):
            schema = Schema.from_stored_schema(stored_schema)
            load_storage = _get_load_storage(destination_caps.preferred_loader_file_format)  # Default load storage, used for empty tables when no data
            normalize_storage = NormalizeStorage(False, normalize_storage_config)

            item_normalizers: Dict[TLoaderFileFormat, ItemsNormalizer] = {}

            def _get_items_normalizer(file_format: TLoaderFileFormat) -> Tuple[ItemsNormalizer, LoadStorage]:
                load_storage = _get_load_storage(file_format)
                if file_format in item_normalizers:
                    return item_normalizers[file_format], load_storage
                klass = ParquetItemsNormalizer if file_format == "parquet" else JsonLItemsNormalizer
                norm = item_normalizers[file_format] = klass(
                    load_storage, normalize_storage, schema, load_id, config
                )
                return norm, load_storage

            try:
                root_tables: Set[str] = set()
                populated_root_tables: Set[str] = set()
                for extracted_items_file in extracted_items_files:
                    line_no: int = 0
                    parsed_file_name = NormalizeStorage.parse_normalize_file_name(extracted_items_file)
                    # normalize table name in case the normalization changed
                    # NOTE: this is the best we can do, until a full lineage information is in the schema
                    root_table_name = schema.naming.normalize_table_identifier(parsed_file_name.table_name)
                    root_tables.add(root_table_name)
                    logger.debug(f"Processing extracted items in {extracted_items_file} in load_id {load_id} with table name {root_table_name} and schema {schema.name}")

                    file_format = parsed_file_name.file_format
                    normalizer, load_storage = _get_items_normalizer(file_format)
                    partial_updates, items_count, r_counts = normalizer(extracted_items_file, root_table_name)
                    schema_updates.extend(partial_updates)
                    total_items += items_count
                    merge_row_count(row_counts, r_counts)
                    if items_count > 0:
                        populated_root_tables.add(root_table_name)
                        logger.debug(f"Processed total {line_no + 1} lines from file {extracted_items_file}, total items {total_items}")
                    # make sure base tables are all covered
                    increase_row_count(row_counts, root_table_name, 0)
                # write empty jobs for tables without items if table exists in schema
                for table_name in root_tables - populated_root_tables:
                    if table_name not in schema.tables:
                        continue
                    logger.debug(f"Writing empty job for table {table_name}")
                    columns = schema.get_table_columns(table_name)
                    load_storage.write_empty_file(load_id, schema.name, table_name, columns)
            except Exception:
                logger.exception(f"Exception when processing file {extracted_items_file}, line {line_no}")
                raise
            finally:
                load_storage.close_writers(load_id)

        logger.info(f"Processed total {total_items} items in {len(extracted_items_files)} files")

        return schema_updates, total_items, load_storage.closed_files(), row_counts

    def update_table(self, schema: Schema, schema_updates: List[TSchemaUpdate]) -> None:
        for schema_update in schema_updates:
            for table_name, table_updates in schema_update.items():
                logger.info(f"Updating schema for table {table_name} with {len(table_updates)} deltas")
                for partial_table in table_updates:
                    # merge columns
                    schema.update_table(partial_table)

    @staticmethod
    def group_worker_files(files: Sequence[str], no_groups: int) -> List[Sequence[str]]:
        # sort files so the same tables are in the same worker
        files = list(sorted(files))

        chunk_size = max(len(files) // no_groups, 1)
        chunk_files = list(chunks(files, chunk_size))
        # distribute the remainder files to existing groups starting from the end
        remainder_l = len(chunk_files) - no_groups
        l_idx = 0
        while remainder_l > 0:
            for idx, file in enumerate(reversed(chunk_files.pop())):
                chunk_files[-l_idx - idx - remainder_l].append(file)  # type: ignore
            remainder_l -=1
            l_idx = idx + 1
        return chunk_files

    def map_parallel(self, schema: Schema, load_id: str, files: Sequence[str]) -> TMapFuncRV:
        workers: int = getattr(self.pool, '_max_workers', 1)
        chunk_files = self.group_worker_files(files, workers)
        schema_dict: TStoredSchema = schema.to_dict()
        param_chunk = [(
            self.config, self.normalize_storage.config, self.load_storage.config, schema_dict, load_id, files
        ) for files in chunk_files]
        row_counts: TRowCount = {}

        # return stats
        schema_updates: List[TSchemaUpdate] = []

        # push all tasks to queue
        tasks = [
            (self.pool.submit(Normalize.w_normalize_files, *params), params) for params in param_chunk
        ]

        while len(tasks) > 0:
            sleep(0.3)
            # operate on copy of the list
            for task in list(tasks):
                pending, params = task
                if pending.done():
                    result: TWorkerRV = pending.result()  # Exception in task (if any) is raised here
                    try:
                        # gather schema from all manifests, validate consistency and combine
                        self.update_table(schema, result[0])
                        schema_updates.extend(result[0])
                        # update metrics
                        self.collector.update("Files", len(result[2]))
                        self.collector.update("Items", result[1])
                        # merge row counts
                        merge_row_count(row_counts, result[3])
                    except CannotCoerceColumnException as exc:
                        # schema conflicts resulting from parallel executing
                        logger.warning(f"Parallel schema update conflict, retrying task ({str(exc)}")
                        # delete all files produced by the task
                        for file in result[2]:
                            os.remove(file)
                        # schedule the task again
                        schema_dict = schema.to_dict()
                        # TODO: it's time for a named tuple
                        params = params[:3] + (schema_dict,) + params[4:]
                        retry_pending: Future[TWorkerRV] = self.pool.submit(Normalize.w_normalize_files, *params)
                        tasks.append((retry_pending, params))
                    # remove finished tasks
                    tasks.remove(task)

        return schema_updates, row_counts

    def map_single(self, schema: Schema, load_id: str, files: Sequence[str]) -> TMapFuncRV:
        result = Normalize.w_normalize_files(
            self.config,
            self.normalize_storage.config,
            self.load_storage.config,
            schema.to_dict(),
            load_id,
            files
        )
        self.update_table(schema, result[0])
        self.collector.update("Files", len(result[2]))
        self.collector.update("Items", result[1])
        return result[0], result[3]

    def spool_files(self, schema_name: str, load_id: str, map_f: TMapFuncType, files: Sequence[str]) -> None:
        schema = Normalize.load_or_create_schema(self.schema_storage, schema_name)
        # process files in parallel or in single thread, depending on map_f
        schema_updates, row_counts = map_f(schema, load_id, files)
        # remove normalizer specific info
        for table in schema.tables.values():
            table.pop("x-normalizer", None)  # type: ignore[typeddict-item]
        logger.info(f"Saving schema {schema_name} with version {schema.version}, writing manifest files")
        # schema is updated, save it to schema volume
        self.schema_storage.save_schema(schema)
        # save schema to temp load folder
        self.load_storage.save_temp_schema(schema, load_id)
        # save schema updates even if empty
        self.load_storage.save_temp_schema_updates(load_id, merge_schema_updates(schema_updates))
        # files must be renamed and deleted together so do not attempt that when process is about to be terminated
        signals.raise_if_signalled()
        logger.info("Committing storage, do not kill this process")
        # rename temp folder to processing
        self.load_storage.commit_temp_load_package(load_id)
        # delete item files to complete commit
        self.normalize_storage.delete_extracted_files(files)
        # log and update metrics
        logger.info(f"Chunk {load_id} processed")
        self._row_counts = row_counts

    def spool_schema_files(self, load_id: str, schema_name: str, files: Sequence[str]) -> str:
        # normalized files will go here before being atomically renamed

        self.load_storage.create_temp_load_package(load_id)
        logger.info(f"Created temp load folder {load_id} on loading volume")
        try:
            # process parallel
            self.spool_files(schema_name, load_id, self.map_parallel, files)
        except CannotCoerceColumnException as exc:
            # schema conflicts resulting from parallel executing
            logger.warning(f"Parallel schema update conflict, switching to single thread ({str(exc)}")
            # start from scratch
            self.load_storage.create_temp_load_package(load_id)
            self.spool_files(schema_name, load_id, self.map_single, files)

        return load_id

    def run(self, pool: Optional[Executor]) -> TRunMetrics:
        # keep the pool in class instance
        self.pool = pool or NullExecutor()
        self._row_counts = {}
        logger.info("Running file normalizing")
        # list files and group by schema name, list must be sorted for group by to actually work
        files = self.normalize_storage.list_files_to_normalize_sorted()
        logger.info(f"Found {len(files)} files")
        if len(files) == 0:
            return TRunMetrics(True, 0)
        # group files by schema
        for schema_name, files_iter in self.normalize_storage.group_by_schema(files):
            schema_files = list(files_iter)
            load_id = str(pendulum.now().timestamp())
            logger.info(f"Found {len(schema_files)} files in schema {schema_name} load_id {load_id}")
            with self.collector(f"Normalize {schema_name} in {load_id}"):
                self.collector.update("Files", 0, len(schema_files))
                self.collector.update("Items", 0)
                self.spool_schema_files(load_id, schema_name, schema_files)
        # return info on still pending files (if extractor saved something in the meantime)
        return TRunMetrics(False, len(self.normalize_storage.list_files_to_normalize_sorted()))

    def get_normalize_info(self) -> NormalizeInfo:
        return NormalizeInfo(row_counts=self._row_counts)
