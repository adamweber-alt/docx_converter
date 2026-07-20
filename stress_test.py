#!/usr/bin/env python3
"""
stress_test.py — throughput/latency load test for the docx->pdf converter.

Usage:
    python stress_test.py --url http://localhost:8000 --file sample.docx \
        --concurrency 8 --requests 100
"""
import argparse
import concurrent.futures
import statistics
import time
import sys
import requests


def send_one(url, file_path, job_num):
    start = time.time()
    try:
        with open(file_path, "rb") as f:
            files = {"file": (f"stress_{job_num}.docx", f,
                               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
            data = {"output_name": f"stress_{job_num}.pdf"}
            resp = requests.post(f"{url}/convert", files=files, data=data, timeout=180)
        elapsed = time.time() - start
        return {
            "job": job_num,
            "status": resp.status_code,
            "elapsed": elapsed,
            "size": len(resp.content) if resp.status_code == 200 else 0,
            "error": None if resp.status_code == 200 else resp.text[:200],
        }
    except requests.exceptions.RequestException as e:
        return {
            "job": job_num, "status": None, "elapsed": time.time() - start,
            "size": 0, "error": str(e),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--file", required=True, help="path to a sample .docx")
    parser.add_argument("--concurrency", type=int, default=4,
                         help="should roughly match/exceed MAX_CONCURRENT_CONVERSIONS to find the ceiling")
    parser.add_argument("--requests", type=int, default=50, help="total requests to send")
    args = parser.parse_args()

    print(f"target={args.url} concurrency={args.concurrency} total_requests={args.requests}")

    # Sanity check the server's up before hammering it
    try:
        h = requests.get(f"{args.url}/health", timeout=5)
        print(f"health check: {h.status_code} {h.json()}")
    except requests.exceptions.RequestException as e:
        print(f"health check failed: {e}", file=sys.stderr)
        sys.exit(1)

    results = []
    wall_start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(send_one, args.url, args.file,  i)
            for i in range(args.requests)
        ]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            r = future.result()
            results.append(r)
            status = r["status"] or "ERR"
            print(f"[{i}/{args.requests}] job={r['job']} status={status} "
                  f"elapsed={r['elapsed']:.2f}s size={r['size']}b"
                  + (f" error={r['error']}" if r["error"] else ""))

    wall_elapsed = time.time() - wall_start

    successes = [r for r in results if r["status"] == 200]
    failures = [r for r in results if r["status"] != 200]
    latencies = [r["elapsed"] for r in successes]

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"total requests:     {len(results)}")
    print(f"successful:         {len(successes)}")
    print(f"failed:             {len(failures)}")
    print(f"wall clock time:    {wall_elapsed:.2f}s")
    if wall_elapsed > 0:
        print(f"throughput:         {len(successes) / wall_elapsed:.2f} conversions/sec")

    if latencies:
        latencies_sorted = sorted(latencies)
        print(f"\nlatency (successful requests only):")
        print(f"  min:    {min(latencies):.2f}s")
        print(f"  mean:   {statistics.mean(latencies):.2f}s")
        print(f"  median: {statistics.median(latencies):.2f}s")
        print(f"  p95:    {latencies_sorted[int(len(latencies_sorted) * 0.95)]:.2f}s")
        print(f"  max:    {max(latencies):.2f}s")

    if failures:
        print(f"\nfailure breakdown:")
        by_status = {}
        for f in failures:
            key = f["status"] or "connection_error"
            by_status[key] = by_status.get(key, 0) + 1
        for status, count in sorted(by_status.items(), key=str):
            print(f"  {status}: {count}")
        print(f"\nfirst few failure details:")
        for f in failures[:5]:
            print(f"  job={f['job']} status={f['status']} error={f['error']}")


if __name__ == "__main__":
    main()
