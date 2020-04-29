"""Based on https://github.com/xvzcf/pq-tls-benchmark/blob/master/emulation-exp/code/kex/experiment.py"""

import csv
import multiprocessing
import os
import io
import subprocess
import itertools
import time
import re
import socket


hostname = "servername"

ALGORITHMS = (
    # Need to specify leaf to construct correct binary directory
    # EXPERIMENT - KEX - LEAF - INT - ROOT
    ('sign', 'X25519', 'RSA2048', 'RSA2048', 'RSA2048'),
    ('sign', 'sikep434compressed', 'Falcon512', 'XMSS', 'RainbowIaCyclic'),
    ('sign', 'sikep434compressed', 'Falcon512', 'XMSS', 'Gemss128'),
    ('sign', 'sikep434compressed', 'Falcon512', 'RainbowIaCyclic', 'RainbowIaCyclic'),
    ('sign', 'sikep434compressed', 'Falcon512', 'RainbowIaCyclic', 'Gemss128'),
    ('sign', 'kyber512', 'Dilithium2', 'Dilithium2', 'Dilithium2',),
    ('sign', 'ntruhps2048509', 'Falcon512', 'Falcon512', 'Falcon512'),
    ('kem', 'X25519', 'RSA2048', 'RSA2048', 'RSA2048'),
    ('kem', 'sikep434compressed', 'Falcon512', 'XMSS', 'RainbowIaCyclic'),
    ('kem', 'sikep434compressed', 'Falcon512', 'XMSS', 'Gemss128'),
    ('kem', 'sikep434compressed', 'Falcon512', 'RainbowIaCyclic', 'RainbowIaCyclic'),
    ('kem', 'sikep434compressed', 'Falcon512', 'Gemss128', 'Gemss128'),
    ('kem', 'kyber512', 'Dilithium2', 'Dilithium2', 'Dilithium2',),
    ('kem', 'ntruhps2048509', 'Falcon512', 'Falcon512', 'Falcon512'),
)

#LATENCIES = ['2.684ms', '15.458ms', '39.224ms', '97.73ms']
LATENCIES = ['15.458ms', '97.73ms']
LOSS_RATES = [0, 5]     # 0.1, 0.5, 1, 1.5, 2, 2.5, 3] + list(range(4, 21)):
NUM_PINGS = 50  # for measuring the practical latency


# xvzcf's experiment used POOL_SIZE = 40
# We start as many servers as clients, so make sure to adjust accordingly
REPETITIONS = 2
POOL_SIZE = 40
SERVER_PORTS = [str(port) for port in range(10000, 10000+POOL_SIZE)]
MEASUREMENTS_PER_PROCESS = 400
MEASUREMENTS_PER_CLIENT = 100

TIMER_REGEX = re.compile(r"(?P<label>[A-Z ]+): (?P<timing>\d+) ns")


def run_subprocess(command, working_dir=".", expected_returncode=0):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=working_dir,
        text=True,
        check=False,
    )
    assert result.returncode == expected_returncode, f"Failed to run '{command}'"
    return result.stdout


def change_qdisc(ns, dev, pkt_loss, delay):
    if pkt_loss == 0:
        command = [
            "ip", "netns", "exec", ns, "tc", "qdisc", "change", "dev", dev,
            "root", "netem", "limit", "1000", "delay", delay,
            "rate", "1000mbit",
        ]
    else:
        command = [
            "ip", "netns", "exec", ns, "tc", "qdisc", "change", "dev", dev,
            "root", "netem", "limit", "1000", "loss", "{0}%".format(pkt_loss),
            "delay", delay, "rate", "1000mbit",
        ]

    # print(" > " + " ".join(command))
    run_subprocess(command)


class ServerProcess(multiprocessing.Process):
    def __init__(self, path, port, type, pipe, cached_int=False):
        super().__init__(daemon=False)
        self.path = path
        self.port = port
        self.pipe = pipe
        self.last_msg = "HANDSHAKE COMPLETED"
        if type == "sign":
            self.servername = "pqtlsserver"
            self.certname = "signing" + (".chain" if not cached_int else "") + ".crt"
            self.keyname = "signing.key"
        else:
            self.servername = "kemtlsserver"
            self.certname = "kem" + (".chain" if not cached_int else "") + ".crt"
            self.keyname = "kem.key"

    def run(self):
        self.server_process = subprocess.Popen(
            [
                "ip", "netns", "exec", "srv_ns",
                f"./{self.servername}",
                "--certs", self.certname,
                "--key", self.keyname,
                "-p", self.port,
                "http",
            ],
            cwd=self.path,
            stdout=subprocess.PIPE,
            bufsize=8192 * 1024,
        )

        # print(f"[+] Launching server on port {self.port}")
        output_reader = io.TextIOWrapper(self.server_process.stdout, newline="\n")
        measurements = {}
        collected_measurements = []
        while (
            len(collected_measurements) < MEASUREMENTS_PER_PROCESS
            and self.server_process.poll() is None
        ):
            line = output_reader.readline()
            if not line:
                break
            line.rstrip()
            result = TIMER_REGEX.match(line)
            if result:
                label = result.group("label")
                measurements[label] = result.group("timing")
                if label == self.last_msg:
                    collected_measurements.append(measurements)
                    measurements = {}

        self.pipe.send(collected_measurements)

        self.server_process.terminate()
        try:
            self.server_process.wait(5)
        except subprocess.TimeoutExpired:
            print("Timeout expired while waiting for server on {port} to terminate")
            self.server_process.kill()


def run_measurement(output_queue, path, port, type, cached_int):
    (inpipe, outpipe) = multiprocessing.Pipe()
    server = ServerProcess(path, port, type, inpipe, cached_int)
    server.start()
    time.sleep(1)

    LAST_MSG = "HANDSHAKE COMPLETED"
    if type == "sign":
        clientname = "pqtlsclient"
        caname = "signing" + ("-int" if cached_int else "-ca") + ".crt"
    else:
        clientname = "kemtlsclient"
        caname = "kem" + ("-int" if cached_int else "-ca") + ".crt"

    client_measurements = []
    # print(f"[+] Starting measurements on port {port}")
    restarts = 0
    allowed_restarts = 2 * MEASUREMENTS_PER_PROCESS / MEASUREMENTS_PER_CLIENT
    while len(client_measurements) < MEASUREMENTS_PER_PROCESS and server.is_alive() and restarts < allowed_restarts:
        try:
            proc_result = subprocess.run(
                [
                    "ip", "netns", "exec", "cli_ns",
                    f"./{clientname}",
                    "--cafile", caname,
                    "--loops",
                    str(min(MEASUREMENTS_PER_PROCESS - len(client_measurements),
                            MEASUREMENTS_PER_CLIENT)),
                    "--port", port,
                    "--no-tickets",
                    "--http",
                    hostname,
                ],
                text=True,
                stdout=subprocess.PIPE,
                timeout=2 * MEASUREMENTS_PER_CLIENT,
                check=False,
                cwd=path,
            )
        except subprocess.TimeoutExpired:
            print("Sever has hung itself, restarting measurements")
            client_measurements.clear()
            server.kill()
            server.join(5)
            server = ServerProcess(path, port, type, inpipe, cached_int)
            server.start()
            time.sleep(1)
        # print(f"[+] Completed measurements on port {port}")
        measurement = {}
        for line in proc_result.stdout.split("\n"):
            result = TIMER_REGEX.match(line)
            if result:
                label = result.group("label")
                measurement[label] = result.group("timing")
                if label == LAST_MSG:
                    client_measurements.append(measurement)
                    measurement = {}
        restarts += 1

    server.join(5)

    server_data = outpipe.recv()
    assert len(server_data) == len(
        client_measurements
    ), f"[!] Process on {port} out of sync {len(server_data)} != {len(client_measurements)}"

    output_queue.put(list(zip(server_data, client_measurements)))


def experiment_run_timers(path, type, cached_int):
    tasks = [(path, port, type, cached_int) for port in SERVER_PORTS]
    output_queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=run_measurement, args=(output_queue, *args))
        for args in tasks
    ]
    results = []
    print(f"[+] Starting processes on {path} for {type}")
    for process in processes:
        process.start()

    # Consume output
    for _i in range(len(processes)):
        results.extend(output_queue.get())

    print(f"[+] Joining processes on {path} for {type}")
    for process in processes:
        process.join(5)

    return results


def get_rtt_ms():
    print("[+] Pinging")
    command = [
        "ip",
        "netns",
        "exec",
        "cli_ns",
        "ping",
        hostname,
        "-c",
        str(NUM_PINGS),
    ]

    # print(" > " + " ".join(command))
    result = run_subprocess(command)

    result_fmt = result.splitlines()[-1].split("/")
    return result_fmt[4]


def write_result(outfile, results):
    server_keys = results[0][0].keys()
    client_keys = results[0][1].keys()
    keys = [f"client {key.lower()}" for key in client_keys] + [
        f"server {key.lower()}" for key in server_keys
    ]

    writer = csv.DictWriter(outfile, keys)
    writer.writeheader()
    for (server_result, client_result) in results:
        row = {f"client {key.lower()}": value for (key, value) in client_result.items()}
        row.update(
            {f"server {key.lower()}": value for (key, value) in server_result.items()}
        )
        writer.writerow(row)


def reverse_resolve_hostname():

    return socket.gethostbyaddr("10.99.0.1")[0]


def get_filename(kex_alg, leaf, intermediate, root, type, cached_int, rtt_ms, pkt_loss):
    fileprefix = f"{kex_alg}_{kex_alg if type == 'kem' else leaf}_{intermediate}"
    if not cached_int:
        fileprefix += f"_{root}"
    fileprefix += f"_{rtt_ms}ms"
    caching_type = "int-chain" if not cached_int else "cached"
    filename = f"data/{type}-{caching_type}/{fileprefix}_{pkt_loss}.csv"
    return filename


def main():
    os.makedirs("data", exist_ok=True)
    os.chown("data", uid=1001, gid=1001)
    for (type, caching) in itertools.product(["kem", "sign"], ["int-chain", "cached"]):
        dirname = os.path.join("data", f"{type}-{caching}")
        os.makedirs(dirname, exist_ok=True)
        os.chown(dirname, uid=1001, gid=1001)

    for latency_ms in LATENCIES:
        # To get actual (emulated) RTT
        change_qdisc("cli_ns", "cli_ve", 0, delay=latency_ms)
        change_qdisc("srv_ns", "srv_ve", 0, delay=latency_ms)
        rtt_ms = get_rtt_ms()

        for (cached_int, (type, kex_alg, leaf, intermediate, root)) in itertools.product([True, False], ALGORITHMS):
            print(
                f"[+] Experiment for {type} {kex_alg} {leaf} {intermediate} "
                f"{root} for {rtt_ms}ms latency with "
                f"{'cached intermediate' if cached_int else 'full cert chain'}"
            )
            experiment_path = os.path.join(
                "bin", f"{kex_alg}-{leaf}-{intermediate}-{root}"
            )
            for pkt_loss in LOSS_RATES:
                print(f"[+] Measuring loss rate {pkt_loss}")
                change_qdisc("cli_ns", "cli_ve", pkt_loss, delay=latency_ms)
                change_qdisc("srv_ns", "srv_ve", pkt_loss, delay=latency_ms)
                result = []
                filename = get_filename(
                    kex_alg, leaf, intermediate, root, type, cached_int, rtt_ms, pkt_loss
                )
                for _ in range(REPETITIONS):
                    result += experiment_run_timers(experiment_path, type, cached_int)
                with open(filename, "w+") as out:
                    write_result(out, result)
                os.chown(filename, uid=1001, gid=1001)


if __name__ == "__main__":
    hostname = reverse_resolve_hostname()
    main()
