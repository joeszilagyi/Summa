#!/usr/bin/env bash
set -euo pipefail

# Neutral path-helper facade for new runtime wrappers. The retained
# index_paths.sh implementation remains the compatibility backend until the
# legacy place/article-shaped driver is fully migrated.
RUNTIME_PATHS_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$RUNTIME_PATHS_LIB_DIR/index_paths.sh"

runtime_repo_root_candidate() {
  index_repo_root_candidate "$@"
}

runtime_repo_root_from_script() {
  index_repo_root_from_script "$@"
}

runtime_workspace_root() {
  index_places_root "$@"
}

runtime_list_workspace_dirs() {
  index_list_place_dirs "$@"
}

runtime_find_workspace_dir() {
  index_find_place_dir "$@"
}

runtime_normalize_workspace_key() {
  index_normalize_place_key "$@"
}

runtime_artifact_timestamp() {
  index_artifact_timestamp "$@"
}

runtime_artifact_unique_suffix() {
  index_artifact_unique_suffix "$@"
}

runtime_allocate_output_path() {
  index_allocate_output_path "$@"
}

runtime_copy_file_no_clobber() {
  index_copy_file_no_clobber "$@"
}

runtime_resolve_workspace_dir() {
  index_resolve_place_dir "$@"
}

runtime_resolve_input_file() {
  index_resolve_article_file "$@"
}
