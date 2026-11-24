"""Cross-check adjustment-factor breaks against dated implementation notices before simulating."""

import io
import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .collect import CACHE, LOCAL, get, universe, write


def check(code):
    frame = pd.read_csv(LOCAL / (code.lower() + ".csv")).set_index("date")
    jumps = set(
        frame.index[
            frame.factor.ne(frame.factor.shift())
            & frame.factor.shift().notna()
            & (frame.index >= "2022")
            & (frame.index < "2025")
        ]
    )
    primary = json.loads((CACHE / "actions" / (code + ".json")).read_bytes())
    known = {
        str(r.get("EX_DIVIDEND_DATE"))[:10]
        for r in primary
        if r.get("NOTICE_DATE") and r.get("EX_DIVIDEND_DATE") and r["NOTICE_DATE"] < r["EX_DIVIDEND_DATE"]
    }
    needed = jumps - known
    extra = []
    if needed:
        path = CACHE / "action_pages" / (code + ".html")
        if not path.exists():
            url = f"https://money.finance.sina.com.cn/corp/go.php/vISSUE_ShareBonus/stockid/{code[2:]}.phtml"
            write(path, get(url).content)
        for table in pd.read_html(io.StringIO(path.read_bytes().decode("gb18030", errors="replace"))):
            cols = [str(col[-1]) if isinstance(col, tuple) else str(col) for col in table.columns]
            if "公告日期" not in cols:
                continue
            for ex_col in ("除权除息日", "除权日"):
                if ex_col not in cols:
                    continue
                for row in table.itertuples(index=False, name=None):
                    ex, notice = str(row[cols.index(ex_col)]), str(row[cols.index("公告日期")])
                    if ex in needed and len(notice) == 10 and notice < ex:
                        record = dict(NOTICE_DATE=notice, EX_DIVIDEND_DATE=ex)
                        if "派息(税前)(元)" in cols:
                            record["PRETAX_BONUS_RMB"] = float(row[cols.index("派息(税前)(元)")])
                            record["BONUS_IT_RATIO"] = float(row[cols.index("送股(股)")]) + float(
                                row[cols.index("转增(股)")]
                            )
                        extra.append(record)
    return code, extra, sorted(needed - {r["EX_DIVIDEND_DATE"] for r in extra})


def main():
    with ThreadPoolExecutor(max_workers=5) as pool:
        records = list(pool.map(check, universe()))
    write(
        CACHE / "action_crosscheck.json",
        json.dumps({c: dict(additions=a, unresolved=u) for c, a, u in records}, indent=2).encode(),
    )
    print(
        "Supplemented", sum(len(a) for _, a, _ in records), "unmatched", [(c, u) for c, _, u in records if u]
    )


if __name__ == "__main__":
    main()
