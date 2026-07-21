from kofin.plugin import context


def _options(monkeypatch, item, enable_delete):
    monkeypatch.setattr(context.settings, "get_bool", lambda key: enable_delete)
    monkeypatch.setattr(context.settings, "localized", lambda i: "L%d" % i)
    monkeypatch.setattr(context.xbmc, "getLocalizedString", lambda i: "K%d" % i)
    return context._manage_options(item)


def test_manage_options_favorite_label_follows_state(monkeypatch):
    not_fav = _options(
        monkeypatch, {"Id": "i1", "UserData": {"IsFavorite": False}}, False
    )
    assert not_fav[0][0] == "K14076"  # Add to favourites
    assert not_fav[0][1] == {"mode": "favorite", "id": "i1"}

    fav = _options(monkeypatch, {"Id": "i1", "UserData": {"IsFavorite": True}}, False)
    assert fav[0][0] == "K14077"  # Remove from favourites
    assert fav[0][1]["mode"] == "unfavorite"


def test_manage_options_delete_gated_by_setting(monkeypatch):
    item = {"Id": "i1", "Name": "The Movie", "UserData": {}}

    off = [params["mode"] for _, params in _options(monkeypatch, item, False)]
    assert off == ["favorite", "settings"]

    on = _options(monkeypatch, item, True)
    modes = [params["mode"] for _, params in on]
    assert modes == ["favorite", "delete", "settings"]
    delete_params = next(params for _, params in on if params["mode"] == "delete")
    assert delete_params["name"] == "The Movie"
