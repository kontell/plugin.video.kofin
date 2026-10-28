# -*- coding: utf-8 -*-
"""The /Items query shapes the API catalogue's enumeration shares.

``info`` is the field list every video listing asks the server for.
"""


def info():
    return (
        "Path,Genres,SortName,OriginalTitle,Studios,Writer,Taglines,LocalTrailerCount,"
        "OfficialRating,CumulativeRunTimeTicks,ItemCounts,"
        "Metascore,AirTime,DateCreated,People,Overview,"
        "Etag,ShortOverview,ProductionLocations,"
        "Tags,ProviderIds,ParentId,RemoteTrailers,SpecialEpisodeNumbers,"
        "MediaSources,VoteCount,RecursiveItemCount,PrimaryImageAspectRatio,"
        "SpecialFeatureCount"
    )


def library_filter(api, parent_id, item_types=None):
    """The filter every library listing shares: this parent, recursive,
    real files only (present, not virtual-unaired), sets uncollapsed."""
    return {
        "userId": api.user_id,
        "ParentId": parent_id,
        "IncludeItemTypes": item_types,
        "CollapseBoxSetItems": False,
        "IsVirtualUnaired": False,
        "LocationTypes": "FileSystem,Remote,Offline",
        "IsMissing": False,
        "Recursive": True,
    }
