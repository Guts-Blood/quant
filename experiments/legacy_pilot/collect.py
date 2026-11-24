"""Fetch a deterministic subset of the dated CSI500 universe. Research dependencies: requests, pandas."""

import hashlib
import io
import json
import os
import re
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "backtests/factor_study_v1"
LOCAL = Path.home() / ".qlib/qlib_data/cn_data/raw_data_back_adjust"
REV = "51f2e1ea9c53fa9a8c47412724ff18559440521d"
BASE = f"https://hf-mirror.com/datasets/OpenFinMind/stock_dataset/resolve/{REV}/"


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, path)


def get(url, params=None):
    for i in range(3):
        try:
            response = requests.get(url, params=params, timeout=(5, 15))
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if i == 2 or (exc.response is not None and exc.response.status_code == 404):
                raise
            time.sleep(1 + i)


def membership():
    file = CACHE / "members.tsv"
    if not file.exists():
        write(file, get(BASE + "qlib/cn_data/instruments/csi500.txt").content)
    return pd.read_csv(file, sep="\t", names=["code", "start", "end"])


def universe():
    m = membership()
    codes = sorted(set(m.loc[(m.end >= "2021-12-30") & (m.start <= "2024-12-31"), "code"]))
    return [
        c
        for c in codes
        if re.fullmatch(r"SH60[0135]\d{3}|SZ(?:00[0123]|30[01])\d{3}", c)
        and (LOCAL / f"{c.lower()}.csv").exists()
        and int(hashlib.sha256(c.encode()).hexdigest(), 16) % 3 == 0
    ]


def download(code, year):
    path = CACHE / "raw" / str(year) / (code + ".csv")
    if path.exists():
        return
    r = get(BASE + f"A股分钟/2000-2024/{year}/5分钟/{code.lower()}.csv")
    d = pd.read_csv(io.BytesIO(r.content), encoding="utf-8-sig")
    if (
        not {"日期", "时间", "开盘", "收盘", "成交量"} <= set(d)
        or d.empty
        or not d["日期"].str.startswith(str(year)).all()
    ):
        raise ValueError("unexpected minute schema or dates")
    write(path, r.content)


def events(code):
    path = CACHE / "actions" / (code + ".json")
    if path.exists():
        return
    p = dict(
        reportName="RPT_SHAREBONUS_DET",
        columns="ALL",
        filter=f'(SECURITY_CODE="{code[2:]}")',
        pageSize=100,
        sortColumns="REPORT_DATE",
        sortTypes="-1",
    )
    result = get("https://datacenter-web.eastmoney.com/api/data/v1/get", p).json()
    if result.get("result"):
        assert result["result"]["pages"] <= 1
        data = result["result"]["data"]
    elif result.get("code") == 9201:
        data = []
    else:
        raise ValueError(str(result)[:200])
    write(path, json.dumps(data, ensure_ascii=False).encode())


def main():
    codes = universe()
    for year in (2021, 2022, 2023, 2024):
        dest = CACHE / "index" / f"{year}.json"
        if not dest.exists():
            start = "20211201" if year == 2021 else f"{year}0101"
            rows = get(
                "https://www.csindex.com.cn/csindex-home/perf/index-perf",
                dict(indexCode="H00905", startDate=start, endDate=f"{year}1231"),
            ).json()["data"]
            assert len(rows) > 15 and all(r["indexCode"] == "H00905" for r in rows)
            write(dest, json.dumps(rows, ensure_ascii=False).encode())
    print("symbols", len(codes), flush=True)
    m = membership()
    jobs = [
        (code, year)
        for code in codes
        for year in (2022, 2023, 2024)
        if ((m.code == code) & (m.start <= f"{year}-12-31") & (m.end >= f"{year-1}-12-01")).any()
    ]
    failures = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        tasks = {pool.submit(download, c, y): (c, y) for c, y in jobs}
        tasks.update({pool.submit(events, c): (c, "actions") for c in codes})
        for i, task in enumerate(as_completed(tasks), 1):
            try:
                task.result()
            except Exception as e:
                failures.append([*tasks[task], str(e)[:350]])
                if len(failures) <= 8:
                    print(*tasks[task], repr(e)[:160], flush=True)
            if i % 50 == 0:
                print(i, "/", len(tasks), "failures", len(failures), flush=True)
    write(CACHE / "fetch-errors.json", json.dumps(failures, indent=2).encode())
    print("finished", len(tasks), len(failures), flush=True)


if __name__ == "__main__":
    main()
