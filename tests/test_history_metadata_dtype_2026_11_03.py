"""CSV-null metadata must accept preferred broker provenance during fallback."""
from io import StringIO

import pandas as pd
import pytest

from data.kite_angel_migration import KiteAngelMigration
from tests.test_broker_capability_priority_2026_10_30 import preferred, backup


@pytest.mark.parametrize("column", ["price_provider", "source", "volume_basis", "volume_provider", "membership_version"])
def test_null_csv_metadata_accepts_string_provenance(column):
    frame = backup().assign(**{column: float("nan")})
    frame = pd.read_csv(StringIO(frame.to_csv()), index_col=0, parse_dates=True)
    assert str(frame[column].dtype) == "float64"
    original = frame.copy(deep=True)
    result = KiteAngelMigration._merge_preferred_index_history(preferred(), frame)
    assert result.price_provider.eq("kite").all()
    assert result.volume_provider.eq("kite").all()
    assert result.volume_complete.all()
    assert result.volume.tolist() == [123., 0., 789.]
    pd.testing.assert_frame_equal(frame, original)


def test_null_metadata_and_backup_only_rows_keep_prices_and_units():
    frame = backup().assign(price_provider=float("nan"))
    candidate = preferred().iloc[1:].copy()
    result = KiteAngelMigration._merge_preferred_index_history(candidate, frame)
    assert pd.isna(result.iloc[0].price_provider)
    assert result.iloc[0].volume_provider == "fyers"
    assert result.iloc[0].volume == 456.
    assert result.iloc[1:].price_provider.eq("kite").all()
    assert result.iloc[1:].volume.tolist() == [0., 789.]


def test_null_boolean_metadata_accepts_verified_preferred_flags():
    frame = backup().assign(volume_complete=float("nan"), price_provider=float("nan"))
    result = KiteAngelMigration._merge_preferred_index_history(preferred(), frame)
    assert result.volume_complete.all()
    assert result.volume.tolist() == [123., 0., 789.]
