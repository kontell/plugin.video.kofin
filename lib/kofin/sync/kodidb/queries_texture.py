get_cache = """
SELECT      cachedurl
FROM        texture
WHERE       url = ?
"""


get_cache_like = """
SELECT      url, cachedurl
FROM        texture
WHERE       url LIKE ?
"""


add_cache = """
INSERT INTO     texture(url, cachedurl, imagehash, lasthashcheck)
VALUES          (?, ?, '', '')
"""


add_size = """
INSERT INTO     sizes(idtexture, size, width, height, usecount, lastusetime)
VALUES          (?, 1, ?, ?, 1, datetime('now'))
"""


delete_cache = """
DELETE FROM     texture
WHERE           url = ?
"""
