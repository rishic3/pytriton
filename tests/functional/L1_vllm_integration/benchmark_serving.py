#!/usr/bin/env python3
# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.
# Copyright (c) 2023, vllm-project. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Benchmark online serving throughput.

Adapted from: https://github.com/vllm-project/vllm/blob/v0.2.1/benchmarks/benchmark_serving.py
"""

import argparse
import asyncio
import json
import random
import time
from typing import AsyncGenerator, List, Tuple

import aiohttp
import numpy as np
from transformers import PreTrainedTokenizerBase  # pytype: disable=import-error
from vllm.transformers_utils.tokenizer import get_tokenizer  # pytype: disable=import-error

# (prompt len, output len, latency)
REQUEST_LATENCY: List[Tuple[int, int, float]] = []
# dict with prompt and output
OUTPUTS = []


def sample_requests(
    dataset_path: str,
    num_requests: int,
    tokenizer: PreTrainedTokenizerBase,
) -> List[Tuple[str, int, int]]:
    # Load the dataset.
    with open(dataset_path) as f:
        dataset = json.load(f)
    # Filter out the conversations with less than 2 turns.
    dataset = [data for data in dataset if len(data["conversations"]) >= 2]
    # Only keep the first two turns of each conversation.
    dataset = [(data["conversations"][0]["value"], data["conversations"][1]["value"]) for data in dataset]

    # Tokenize the prompts and completions.
    prompts = [prompt for prompt, _ in dataset]
    prompt_token_ids = tokenizer(prompts).input_ids
    completions = [completion for _, completion in dataset]
    completion_token_ids = tokenizer(completions).input_ids
    tokenized_dataset = []
    for i in range(len(dataset)):
        output_len = len(completion_token_ids[i])
        tokenized_dataset.append((prompts[i], prompt_token_ids[i], output_len))

    # Filter out too long sequences.
    filtered_dataset: List[Tuple[str, int, int]] = []
    for prompt, prompt_token_ids, output_len in tokenized_dataset:
        prompt_len = len(prompt_token_ids)
        if prompt_len < 4 or output_len < 4:
            # Prune too short sequences.
            # This is because TGI causes errors when the input or output length
            # is too short.
            continue
        if prompt_len > 1024 or prompt_len + output_len > 2048:
            # Prune too long sequences.
            continue
        filtered_dataset.append((prompt, prompt_len, output_len))

    # Sample the requests.
    sampled_requests = random.sample(filtered_dataset, num_requests)
    return sampled_requests


async def get_request(
    input_requests: List[Tuple[str, int, int]],
    request_rate: float,
) -> AsyncGenerator[Tuple[str, int, int], None]:
    input_requests = iter(input_requests)
    for request in input_requests:
        yield request

        if request_rate == float("inf"):
            # If the request rate is infinity, then we don't need to wait.
            continue
        # Sample the request interval from the exponential distribution.
        interval = np.random.exponential(1.0 / request_rate)
        # The next request will be sent after the interval.
        await asyncio.sleep(interval)


async def send_request(
    backend: str,
    api_url: str,
    prompt: str,
    prompt_len: int,
    output_len: int,
    best_of: int,
) -> None:
    request_start_time = time.perf_counter()

    headers = {"User-Agent": "Benchmark Client"}
    if backend in ["vllm", "triton"]:
        pload = {
            "prompt": prompt,
            "n": 1,
            "best_of": best_of,
            "temperature": 0.0,  # force greedy decoding for same results
            "top_p": 1.0,
            "max_tokens": output_len,
            "ignore_eos": True,
            "stream": False,
        }
    elif backend == "tgi":
        params = {
            "best_of": best_of,
            "max_new_tokens": output_len,
            "do_sample": True,
        }
        pload = {
            "inputs": prompt,
            "parameters": params,
        }
    else:
        raise ValueError(f"Unknown backend: {backend}")

    timeout = aiohttp.ClientTimeout(total=3 * 3600)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            async with session.post(api_url, headers=headers, json=pload) as response:
                chunks = []
                async for chunk, _ in response.content.iter_chunks():
                    chunks.append(chunk)
            output = b"".join(chunks).decode("utf-8")
            output = json.loads(output)

            # Re-send the request if it failed.
            if "error" not in output:
                break

    request_end_time = time.perf_counter()
    request_latency = request_end_time - request_start_time
    REQUEST_LATENCY.append((prompt_len, output_len, request_latency))

    if isinstance(output["text"], list):  # TODO: due to potential bug in triton, under investigation
        added = output["text"][0][len(prompt) :]
    else:
        added = output["text"][len(prompt) :]
    OUTPUTS.append({"prompt": prompt, "added": added})


async def benchmark(
    backend: str,
    api_url: str,
    input_requests: List[Tuple[str, int, int]],
    best_of: int,
    request_rate: float,
) -> None:
    tasks: List[asyncio.Task] = []
    async for request in get_request(input_requests, request_rate):
        prompt, prompt_len, output_len = request
        task = asyncio.create_task(send_request(backend, api_url, prompt, prompt_len, output_len, best_of))
        tasks.append(task)
    await asyncio.gather(*tasks)


def main(args: argparse.Namespace):
    print(args)  # noqa: T201
    random.seed(args.seed)
    np.random.seed(args.seed)

    model_name = args.tokenizer.replace("-", "_")

    api_url = {
        "vllm": f"http://{args.host}:{args.port}/generate",
        "tgi": f"http://{args.host}:{args.port}/generate",
        "triton": f"http://{args.host}:{args.port}/v2/models/{model_name}/generate",
    }[args.backend]
    tokenizer = get_tokenizer(args.tokenizer, trust_remote_code=args.trust_remote_code)
    input_requests = sample_requests(args.dataset, args.num_prompts, tokenizer)

    benchmark_start_time = time.perf_counter()
    asyncio.run(benchmark(args.backend, api_url, input_requests, args.best_of, args.request_rate))
    benchmark_end_time = time.perf_counter()
    benchmark_time = benchmark_end_time - benchmark_start_time
    print(f"Total time: {benchmark_time:.2f} s")  # noqa: T201
    print(f"Throughput: {args.num_prompts / benchmark_time:.2f} requests/s")  # noqa: T201

    # Compute the latency statistics.
    avg_latency = np.mean([latency for _, _, latency in REQUEST_LATENCY])
    print(f"Average latency: {avg_latency:.2f} s")  # noqa: T201
    avg_per_token_latency = np.mean([
        latency / (prompt_len + output_len) for prompt_len, output_len, latency in REQUEST_LATENCY
    ])
    print(f"Average latency per token: {avg_per_token_latency:.2f} s")  # noqa: T201
    avg_per_output_token_latency = np.mean([latency / output_len for _, output_len, latency in REQUEST_LATENCY])
    print("Average latency per output token: " f"{avg_per_output_token_latency:.2f} s")  # noqa: T201

    with open(f"outputs-{args.backend}.jsonl", "w") as output_file:
        output_file.writelines([json.dumps(entry) + "\n" for entry in OUTPUTS])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark the online serving throughput.")
    parser.add_argument("--backend", type=str, default="vllm", choices=["vllm", "tgi", "triton"])
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dataset", type=str, required=True, help="Path to the dataset.")
    parser.add_argument("--tokenizer", type=str, required=True, help="Name or path of the tokenizer.")
    parser.add_argument(
        "--best-of", type=int, default=1, help="Generates `best_of` sequences per prompt and returns the best one."
    )
    parser.add_argument("--num-prompts", type=int, default=1000, help="Number of prompts to process.")
    parser.add_argument(
        "--request-rate",
        type=float,
        default=float("inf"),
        help="Number of requests per second. If this is inf, "
        "then all the requests are sent at time 0. "
        "Otherwise, we use Poisson process to synthesize "
        "the request arrival times.",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--trust-remote-code", action="store_true", help="trust remote code from huggingface")
    args = parser.parse_args()
    main(args)
