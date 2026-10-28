get_view = """
SELECT      *
FROM        view
WHERE       view_id = ?
"""

get_playlist_state = """
SELECT      media_type, filename, checksum
FROM        playlist_state
WHERE       jellyfin_id = ?
"""
get_playlist_states = """
SELECT      jellyfin_id, media_type, filename, checksum
FROM        playlist_state
"""
add_playlist_state = """
INSERT OR REPLACE INTO      playlist_state(jellyfin_id, media_type, filename, checksum)
VALUES                      (?, ?, ?, ?)
"""
delete_playlist_state = """
DELETE FROM     playlist_state
WHERE           jellyfin_id = ?
"""
delete_playlist_states = """
DELETE FROM     playlist_state
"""
