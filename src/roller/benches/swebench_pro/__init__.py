"""SWE-Bench Pro adapter and official-image scoring integration.

The package facade keeps the original ``roller.benches.swebench_pro`` import
surface stable while implementation details live in focused modules.
"""

from roller.benches.swebench_pro.adapter import SWEBenchProAdapter
from roller.benches.swebench_pro.constants import (
    BASELINE_REF,
    DEFAULT_TIMEOUT_SECONDS,
    EVALUATION_PROTOCOL_REVISION,
    GRADE_DIR_NAME,
    GRADE_TIMEOUT_SECONDS,
    OUTPUT_DIR_NAME,
    SOURCE_IMAGE_PREFIX,
    TASK_SCHEMA,
    TEST_TIMEOUT_SECONDS,
    WORKSPACE_CONTAINER_PATH,
)
from roller.benches.swebench_pro.grader_runtime import (
    ContainerGrader,
    SWEBenchProGradedHarness,
    _GRADER_SOURCE,
    _strip_binary_hunks,
)
from roller.benches.swebench_pro.images import (
    _DERIVED_IMAGES,
    _image_id,
    ensure_harness_image,
)
from roller.benches.swebench_pro.task_parser import (
    _clean_text,
    _dataset_path,
    _load_dataset,
    _string_list,
    _test_files,
)
from roller.benches.swebench_pro.workspace import (
    _default_cache_root,
    _materialize_test_patch,
    _setup_command,
    _test_fixture_patch,
)

__all__ = [
    "SWEBenchProAdapter",
    "SWEBenchProGradedHarness",
    "ensure_harness_image",
]
