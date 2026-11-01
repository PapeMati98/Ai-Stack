# office wait benchmark

machine: win32 cpu_count=24
python: 3.11.15

## A. wake latency

| target | agents | sent after | elapsed at send | min | mean | max | cpu/waiter | polls |
|---|---|---|---|---|---|---|---|---|
| channel | 2 | 0.3 s | 0.3-0.32 s | 29.8 ms | 38.0 ms | 46.1 ms | 7.8 ms | 7.5 |
| channel | 2 | 3.0 s | 3.0-3.03 s | 38.1 ms | 271.8 ms | 505.4 ms | 7.8 ms | 25.5 |
| channel | 2 | 12.0 s | 12.0-12.02 s | 33.9 ms | 34.6 ms | 35.3 ms | 23.4 ms | 40.0 |
| channel | 5 | 0.3 s | 0.3-0.39 s | 46.6 ms | 51.9 ms | 60.8 ms | 3.1 ms | 8.6 |
| channel | 5 | 3.0 s | 3.0-3.11 s | 34.6 ms | 375.0 ms | 494.7 ms | 15.6 ms | 25.8 |
| channel | 5 | 12.0 s | 12.0-12.09 s | 17.9 ms | 1193.7 ms | 1991.3 ms | 21.9 ms | 40.6 |
| channel | 10 | 0.3 s | 0.3-0.5 s | 18.5 ms | 30.9 ms | 52.6 ms | 3.1 ms | 9.4 |
| channel | 10 | 3.0 s | 3.0-3.23 s | 36.0 ms | 361.2 ms | 492.6 ms | 17.2 ms | 25.9 |
| channel | 10 | 12.0 s | 12.0-12.2 s | 20.5 ms | 1534.6 ms | 1989.5 ms | 20.3 ms | 40.8 |
| inbox | 2 | 0.3 s | 0.3-0.32 s | 40.1 ms | 52.6 ms | 65.2 ms | 7.8 ms | 8.0 |
| inbox | 2 | 3.0 s | 3.0-3.02 s | 32.8 ms | 38.2 ms | 43.6 ms | 23.4 ms | 25.0 |
| inbox | 2 | 12.0 s | 12.0-12.02 s | 27.4 ms | 36.8 ms | 46.1 ms | 39.1 ms | 40.0 |
| inbox | 5 | 0.3 s | 0.32-0.38 s | 22.6 ms | 42.4 ms | 61.3 ms | 6.2 ms | 8.6 |
| inbox | 5 | 3.0 s | 3.01-3.07 s | 29.8 ms | 397.5 ms | 512.2 ms | 25.0 ms | 25.8 |
| inbox | 5 | 12.0 s | 12.0-12.08 s | 32.4 ms | 1214.3 ms | 2013.3 ms | 25.0 ms | 40.6 |
| inbox | 10 | 0.3 s | 0.39-0.46 s | 19.5 ms | 45.4 ms | 69.0 ms | 7.8 ms | 9.9 |
| inbox | 10 | 3.0 s | 3.02-3.13 s | 13.7 ms | 407.9 ms | 500.8 ms | 23.4 ms | 25.9 |
| inbox | 10 | 12.0 s | 12.03-12.15 s | 20.7 ms | 1564.6 ms | 1985.9 ms | 25.0 ms | 40.8 |

## B. idle CPU

| agents | idle | cpu/waiter min | mean | max | cpu % | polls |
|---|---|---|---|---|---|---|
| 1 | 60.0 s | 31.2 ms | 31.2 ms | 31.2 ms | 0.052 % | 64.0 |
| 10 | 60.0 s | 0.0 ms | 23.4 ms | 46.9 ms | 0.039 % | 64.0 |

## C. backlog

- 163 messages of 16 KB written in 4.54 s; 2636362 bytes retained over 3 generations
- first probe with unread backlog: message in 0.9 ms
- drain of 163 messages: 0.048 s
- one poll with the cursor at the end of the full live file: timeout in 10.46 ms
- wake latency with that backlog: 69.0 ms (message)

```json
{
  "backlog": {
    "drain_messages": 163,
    "drain_s": 0.048,
    "fill_s": 4.54,
    "first_probe_code": "message",
    "first_probe_ms": 0.9,
    "generations": 3,
    "messages_written": 163,
    "poll_on_full_live_file_code": "timeout",
    "poll_on_full_live_file_ms": 10.46,
    "retained_bytes": 2636362,
    "wake_code": "message",
    "wake_latency_ms": 69.0
  },
  "idle_cpu": [
    {
      "agents": 1,
      "codes": [
        "timeout"
      ],
      "cpu_ms_per_waiter": {
        "max_ms": 31.2,
        "mean_ms": 31.2,
        "min_ms": 31.2
      },
      "cpu_percent_per_waiter": 0.052,
      "idle_s": 60.0,
      "polls_per_waiter": 64.0,
      "waited_s": 60.0
    },
    {
      "agents": 10,
      "codes": [
        "timeout"
      ],
      "cpu_ms_per_waiter": {
        "max_ms": 46.9,
        "mean_ms": 23.4,
        "min_ms": 0.0
      },
      "cpu_percent_per_waiter": 0.039,
      "idle_s": 60.0,
      "polls_per_waiter": 64.0,
      "waited_s": 60.0
    }
  ],
  "machine": "win32 cpu_count=24",
  "python": "3.11.15",
  "wait_tiers": "50 ms (<1 s), 500 ms (<10 s), 2 s (>=10 s)",
  "wake": [
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 7.8,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.3,
        0.32
      ],
      "latency": {
        "max_ms": 46.1,
        "mean_ms": 38.0,
        "min_ms": 29.8
      },
      "polls_per_waiter": 7.5,
      "target": "channel"
    },
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 7.8,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.0,
        3.03
      ],
      "latency": {
        "max_ms": 505.4,
        "mean_ms": 271.8,
        "min_ms": 38.1
      },
      "polls_per_waiter": 25.5,
      "target": "channel"
    },
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 23.4,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.0,
        12.02
      ],
      "latency": {
        "max_ms": 35.3,
        "mean_ms": 34.6,
        "min_ms": 33.9
      },
      "polls_per_waiter": 40.0,
      "target": "channel"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 3.1,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.3,
        0.39
      ],
      "latency": {
        "max_ms": 60.8,
        "mean_ms": 51.9,
        "min_ms": 46.6
      },
      "polls_per_waiter": 8.6,
      "target": "channel"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 15.6,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.0,
        3.11
      ],
      "latency": {
        "max_ms": 494.7,
        "mean_ms": 375.0,
        "min_ms": 34.6
      },
      "polls_per_waiter": 25.8,
      "target": "channel"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 21.9,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.0,
        12.09
      ],
      "latency": {
        "max_ms": 1991.3,
        "mean_ms": 1193.7,
        "min_ms": 17.9
      },
      "polls_per_waiter": 40.6,
      "target": "channel"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 3.1,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.3,
        0.5
      ],
      "latency": {
        "max_ms": 52.6,
        "mean_ms": 30.9,
        "min_ms": 18.5
      },
      "polls_per_waiter": 9.4,
      "target": "channel"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 17.2,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.0,
        3.23
      ],
      "latency": {
        "max_ms": 492.6,
        "mean_ms": 361.2,
        "min_ms": 36.0
      },
      "polls_per_waiter": 25.9,
      "target": "channel"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 20.3,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.0,
        12.2
      ],
      "latency": {
        "max_ms": 1989.5,
        "mean_ms": 1534.6,
        "min_ms": 20.5
      },
      "polls_per_waiter": 40.8,
      "target": "channel"
    },
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 7.8,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.3,
        0.32
      ],
      "latency": {
        "max_ms": 65.2,
        "mean_ms": 52.6,
        "min_ms": 40.1
      },
      "polls_per_waiter": 8.0,
      "target": "inbox"
    },
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 23.4,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.0,
        3.02
      ],
      "latency": {
        "max_ms": 43.6,
        "mean_ms": 38.2,
        "min_ms": 32.8
      },
      "polls_per_waiter": 25.0,
      "target": "inbox"
    },
    {
      "agents": 2,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 39.1,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.0,
        12.02
      ],
      "latency": {
        "max_ms": 46.1,
        "mean_ms": 36.8,
        "min_ms": 27.4
      },
      "polls_per_waiter": 40.0,
      "target": "inbox"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 6.2,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.32,
        0.38
      ],
      "latency": {
        "max_ms": 61.3,
        "mean_ms": 42.4,
        "min_ms": 22.6
      },
      "polls_per_waiter": 8.6,
      "target": "inbox"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 25.0,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.01,
        3.07
      ],
      "latency": {
        "max_ms": 512.2,
        "mean_ms": 397.5,
        "min_ms": 29.8
      },
      "polls_per_waiter": 25.8,
      "target": "inbox"
    },
    {
      "agents": 5,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 25.0,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.0,
        12.08
      ],
      "latency": {
        "max_ms": 2013.3,
        "mean_ms": 1214.3,
        "min_ms": 32.4
      },
      "polls_per_waiter": 40.6,
      "target": "inbox"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 7.8,
      "delay_s": 0.3,
      "elapsed_at_send_s": [
        0.39,
        0.46
      ],
      "latency": {
        "max_ms": 69.0,
        "mean_ms": 45.4,
        "min_ms": 19.5
      },
      "polls_per_waiter": 9.9,
      "target": "inbox"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 23.4,
      "delay_s": 3.0,
      "elapsed_at_send_s": [
        3.02,
        3.13
      ],
      "latency": {
        "max_ms": 500.8,
        "mean_ms": 407.9,
        "min_ms": 13.7
      },
      "polls_per_waiter": 25.9,
      "target": "inbox"
    },
    {
      "agents": 10,
      "codes": [
        "message"
      ],
      "cpu_ms_per_waiter": 25.0,
      "delay_s": 12.0,
      "elapsed_at_send_s": [
        12.03,
        12.15
      ],
      "latency": {
        "max_ms": 1985.9,
        "mean_ms": 1564.6,
        "min_ms": 20.7
      },
      "polls_per_waiter": 40.8,
      "target": "inbox"
    }
  ]
}
```
