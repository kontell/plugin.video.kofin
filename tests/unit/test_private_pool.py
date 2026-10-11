"""The plugin process keeps one private-database connection per thread, and
a service thread may hold one for the length of a block."""

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


def test_a_held_connection_serves_every_block_and_closes_with_the_hold(tmp_path):
    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        with private.Database() as before:
            outside = before.conn
        with private.hold_connections():
            with private.Database() as first:
                held = first.conn
                first.cursor.execute("CREATE TABLE t (n INTEGER)")
                first.cursor.execute("INSERT INTO t VALUES (1)")
            assert held is not outside and not held.in_transaction
            # A hold inside a hold is the same hold.
            with private.hold_connections():
                with private.Database() as second:
                    assert second.conn is held
            with private.Database() as third:
                assert third.conn is held
                assert third.cursor.execute("SELECT n FROM t").fetchall() == [(1,)]
        try:
            held.execute("SELECT 1")
        except Exception as error:
            assert "closed" in str(error).lower()
        else:
            raise AssertionError("held connection outlived its hold")
        with private.Database() as after:
            assert after.conn is not held
            assert after.cursor.execute("SELECT n FROM t").fetchall() == [(1,)]
    finally:
        private.reset_overrides()


def test_a_hold_is_its_own_threads_and_a_failed_block_leaves_it_usable(tmp_path):
    import threading

    private.reset_overrides()
    private.set_path_override("kofin", str(tmp_path / "kofin.db"))
    try:
        with private.hold_connections():
            with private.Database() as first:
                held = first.conn
                first.cursor.execute("CREATE TABLE t (n INTEGER)")
            seen = []

            def elsewhere():
                with private.Database() as other:
                    seen.append(other.conn is held)
                    other.cursor.execute("INSERT INTO t VALUES (7)")

            worker = threading.Thread(target=elsewhere)
            worker.start()
            worker.join()
            assert seen == [False]
            try:
                with private.Database() as failing:
                    failing.cursor.execute("INSERT INTO t VALUES (8)")
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            assert not held.in_transaction
            with private.Database() as check:
                assert check.conn is held
                assert check.cursor.execute("SELECT n FROM t").fetchall() == [(7,)]
    finally:
        private.reset_overrides()
