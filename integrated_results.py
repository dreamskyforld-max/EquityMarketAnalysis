import subprocess, os, sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(SCRIPT_DIR, ".venv", "bin", "python3")

def run_script(script_name, output_file, extra_args=None):
    script_path = os.path.join(SCRIPT_DIR, script_name)
    output_path = os.path.join(SCRIPT_DIR, output_file)
    cmd = [VENV_PYTHON, script_path]
    if extra_args:
        if isinstance(extra_args, list):
            cmd.extend(extra_args)
        else:
            cmd.append(extra_args)
    print(f"▶ 正在运行 {script_name} {' '.join(cmd[2:])} ...")
    with open(output_path, "w", encoding="utf-8") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)

    # 使用 sed 彻底删除包含 [open_context_base.py] 的日志行
    subprocess.run(['sed', '-i', '', '/open_context_base/d', output_path], check=False)

    print(f"  ✅ 已保存到 {output_file}")

if __name__ == "__main__":
    STOCK_CODE = sys.argv[1] if len(sys.argv) > 1 else "HK.00700"
    run_script("get_quote.py", "quote_data.txt", [STOCK_CODE, "HK.800000"])
    run_script("get_realtime_order_size.py", "order_size_data.txt", STOCK_CODE)
    run_script("get_benchmark.py", "benchmark_data.txt")
    run_script("get_excess_return.py", "excess_return_data.txt", [STOCK_CODE, "HK.800000"])

    print(f"\n🎉 全部数据采集完成！目标标的：{STOCK_CODE}")
    print("下一步：运行 sync_to_server.sh 同步到服务器")