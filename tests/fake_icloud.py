"""Fake CloudKit endpoints for pyicloud's (legacy-path) PhotosService.

pyicloud falls back to plain ``session.post(...).json()`` calls when the
session is a mock, which lets the real pyicloud Photos code run against these
canned CloudKit responses.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import MagicMock

PRIMARY_ZONE_NAME = "PrimarySync"
SHARED_ZONE = "SharedSync-11111111-2222-3333-4444-555555555555"


def _photo(
    rec: str, filename: str, date_ms: int, fingerprint: str, uti: str = "public.heic"
) -> list[dict[str, Any]]:
    master = {
        "recordName": f"M-{rec}",
        "recordType": "CPLMaster",
        "fields": {
            "filenameEnc": {"value": base64.b64encode(filename.encode()).decode()},
            "originalCreationDate": {"value": date_ms},
            "itemType": {"value": uti},
            "resOriginalRes": {
                "value": {"downloadURL": f"https://cdn.example/{rec}/orig", "size": 10}
            },
            "resOriginalFileType": {"value": uti},
            "resOriginalFingerprint": {"value": fingerprint},
            "resJPEGMedRes": {
                "value": {"downloadURL": f"https://cdn.example/{rec}/med", "size": 5}
            },
            "resJPEGMedFileType": {"value": "public.jpeg"},
            "resJPEGThumbRes": {
                "value": {"downloadURL": f"https://cdn.example/{rec}/thumb", "size": 1}
            },
            "resJPEGThumbFileType": {"value": "public.jpeg"},
        },
    }
    asset = {
        "recordName": rec,
        "recordType": "CPLAsset",
        "fields": {
            "masterRef": {"value": {"recordName": f"M-{rec}"}},
            "assetDate": {"value": date_ms},
        },
    }
    return [master, asset]


# zone -> smart album list type -> records
DATA: dict[str, dict[str, list[dict[str, Any]]]] = {
    PRIMARY_ZONE_NAME: {
        "CPLAssetAndMasterInSmartAlbumByAssetDate": _photo(
            "P1", "personal.jpg", 1_600_000_000_000, "fp-p1", "public.jpeg"
        )
        + _photo("P2", "dup.heic", 1_650_000_000_000, "fp-dup"),
    },
    SHARED_ZONE: {
        "CPLAssetAndMasterInSmartAlbumByAssetDate": _photo(
            "S1", "shared-fav.heic", 1_700_000_000_000, "fp-s1"
        )
        + _photo("S2", "dup.heic", 1_650_000_000_000, "fp-dup"),
        "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted": _photo(
            "S1", "shared-fav.heic", 1_700_000_000_000, "fp-s1"
        )
        + _photo("S2", "dup.heic", 1_650_000_000_000, "fp-dup")
        + _photo("S3", "not-fav.heic", 1_710_000_000_000, "fp-s3"),
    },
}


SHARED_ALBUM_GUID = "5FD857E3-B35A-4442-93BD-001C8A1A9928"
SHARED_ALBUM_RECORDS = _photo(
    "A1", "IMG_0356.HEIC", 1_720_000_000_000, "fp-a1"
) + _photo("A2", "Ferien Übersicht.JPG", 1_730_000_000_000, "fp-a2", "public.jpeg")
SHARED_ALBUMS = [
    {
        "albumguid": SHARED_ALBUM_GUID,
        "albumlocation": "https://streams.example/album/",
        "albumctag": "ctag",
        "ownerdsid": "123",
        "sharingtype": "owned",
        "iswebuploadsupported": False,
        "attributes": {
            "name": "Bilderrahmen",
            "creationDate": "1700000000000",
            "allowcontributions": False,
            "ispublic": False,
        },
    }
]


def _json_response(data: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = data
    return response


def _fake_post(url: str, json: dict[str, Any] | None = None, **_: Any) -> MagicMock:
    if "webgetalbumslist" in url:
        return _json_response({"albums": SHARED_ALBUMS})
    if "webgetassetcount" in url:
        return _json_response({"albumassetcount": len(SHARED_ALBUM_RECORDS) // 2})
    if "webgetassets" in url:
        assert json is not None and json["albumguid"] == SHARED_ALBUM_GUID
        offset = int(json["offset"])
        return _json_response({"records": SHARED_ALBUM_RECORDS if offset == 0 else []})
    if "zones/list" in url:
        return _json_response(
            {
                "zones": [
                    {"zoneID": {"zoneName": PRIMARY_ZONE_NAME}},
                    {
                        "zoneID": {
                            "zoneName": SHARED_ZONE,
                            "zoneType": "REGULAR_CUSTOM_ZONE",
                        }
                    },
                    {"zoneID": {"zoneName": "SomeOtherZone"}},
                ]
            }
        )
    assert json is not None
    if "query/batch" in url:
        zone = json["batch"][0]["zoneID"]["zoneName"]
        count = len(DATA[zone]["CPLAssetAndMasterInSmartAlbumByAssetDate"]) // 2
        return _json_response(
            {"batch": [{"records": [{"fields": {"itemCount": {"value": count}}}]}]}
        )
    record_type = json["query"]["recordType"]
    if record_type == "CheckIndexingState":
        return _json_response(
            {"records": [{"fields": {"state": {"value": "FINISHED"}}}]}
        )
    zone = json["zoneID"]["zoneName"]
    return _json_response({"records": DATA.get(zone, {}).get(record_type, [])})
