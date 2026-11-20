#!/usr/bin/env python3
"""
build_facts.py  --  Turn a compact text extract of SEC data into the JSON
format edgar_statements.py and edgar_quarterly.py read with --facts-file.

Used when the SEC download has to come through the browser instead of
Terminal. Each line of the extract is:  Tag|unit|start,end,value,form;...
(dates as YYMMDD, values with an m or k suffix for millions or thousands).

Usage:  python build_facts.py extract.txt "COMPANY NAME" CIK EXPECTED_HASH
"""
import json
import sys


def checksum(text):
    h = 5381
    for ch in text:
        h = ((h * 33) ^ ord(ch)) & 0xFFFFFFFF
    return h


def main():
    path, name, cik, want = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
    text = open(path).read().rstrip("\n")
    got = checksum(text)
    if got != want:
        sys.exit(f"CHECKSUM MISMATCH: got {got} ({len(text)} chars), expected {want}. Nothing written.")
    d = lambda s: f"20{s[0:2]}-{s[2:4]}-{s[4:6]}"
    gaap = {}
    for line in text.split("\n"):
        tag, unit, body = line.split("|", 2)
        out = []
        for e in body.split(";"):
            s, en, v, f = e.split(",")
            if v.endswith("m"):
                val = int(v[:-1]) * 1_000_000
            elif v.endswith("k"):
                val = int(v[:-1]) * 1_000
            else:
                val = float(v) if ("." in v or "e" in v.lower()) else int(v)
            rec = {"end": d(en), "val": val, "form": "10-K" if f == "K" else "10-Q", "filed": "2000-01-01"}
            if s:
                rec["start"] = d(s)
            out.append(rec)
        gaap.setdefault(tag, {"units": {}})["units"][unit] = out
    dest = f"edgar_cache/facts_{cik}.json"
    json.dump({"entityName": name, "cik": cik,
               "_note": "Reduced copy of SEC companyfacts fetched through the browser: only the tags the engine uses, one value per period.",
               "facts": {"us-gaap": gaap}}, open(dest, "w"))
    print(f"ok: {len(text)} chars, {len(gaap)} tags -> {dest}")


if __name__ == "__main__":
    main()
