# Confidence Model

`tools/source_db_tools/confidence_model.py` centralizes score parsing,
dimension-name validation, and score-band checks used by source-db profile
validation.

Current contract:

- scores are numeric values in the closed range `0.0` to `1.0`
- known dimensions are listed in `CONFIDENCE_DIMENSIONS`
- score-band mapping is defined by `CONFIDENCE_BANDS`
- supported score field names are listed in `CONFIDENCE_SCORE_KEYS`

The helper walks nested canonical records and emits structured issues for:

- invalid numeric confidence values
- unknown confidence dimensions
- mismatched explicit confidence bands
- missing confidence values on configured paths

When changing score dimensions, bands, or policy defaults, keep this helper and
the profile-validation tests aligned. This module is a pure validation helper;
it does not write to SQLite and it does not assign scores automatically.
