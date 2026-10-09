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


def test_pooled_block_ends_its_transaction_whether_or_not_it_wrote(tmp_path):
    """total_changes counts since the connection opened; on a pooled one a
    block is judged by its delta, and a block that began a transaction and
    changed nothing still ends it, or the next block runs inside it."""
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        private.pool_connections(True)
        with private.Database() as first:
            first.cursor.execute("CREATE TABLE t (n INTEGER)")
            first.cursor.execute("INSERT INTO t VALUES (1)")
        conn = first.conn
        assert not conn.in_transaction
        with private.Database() as idle:
            idle.cursor.execute("BEGIN IMMEDIATE")
        assert not conn.in_transaction
        try:
            with private.Database() as failing:
                failing.cursor.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not conn.in_transaction
        with private.Database() as check:
            assert check.cursor.execute("SELECT n FROM t ORDER BY n").fetchall() == [
                (1,)
            ]
    finally:
        private.pool_connections(False)
        private.reset_overrides()
