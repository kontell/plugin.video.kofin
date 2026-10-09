"""The plugin process keeps one private-database connection per thread."""

from kofin.sync import private


def test_pooled_connections_are_reused_and_released(tmp_path, monkeypatch):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        private.pool_connections(True)
        with private.Database() as first:
            first_conn = first.conn
            first.cursor.execute("SELECT 1").fetchone()
        with private.Database() as second:
            assert second.conn is first_conn
            second.cursor.execute("SELECT 1").fetchone()
        private.pool_connections(False)
        try:
            first_conn.execute("SELECT 1")
        except Exception as error:  # a closed connection refuses work
            assert "closed" in str(error).lower()
        else:
            raise AssertionError("pooled connection was not closed")
        with private.Database() as third:
            assert third.conn is not first_conn
            third.cursor.execute("SELECT 1").fetchone()
    finally:
        private.pool_connections(False)
        private.reset_overrides()
