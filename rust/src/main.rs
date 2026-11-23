use axum::{
    extract::{Query, State},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{
    env,
    net::SocketAddr,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use sysinfo::{Pid, System};

#[derive(Deserialize)]
struct ComputeParams {
    n: Option<u32>,
}

#[derive(Serialize)]
struct ComputeResponse {
    lang: &'static str,
    n: u32,
    primes: u64,
    elapsed_ms: f64,
}

#[derive(Serialize)]
struct HealthResponse {
    status: &'static str,
    lang: &'static str,
}

#[derive(Clone, Serialize)]
struct MetricSample {
    elapsed_sec: f64,
    cpu_percent: f64,
    mem_percent: f64,
    mem_used_mb: f64,
    process_mem_mb: f64,
    requests_so_far: u64,
}

#[derive(Serialize)]
struct MetricsSummary {
    duration_sec: f64,
    samples_count: usize,
    total_requests: u64,
    cpu_percent_avg: f64,
    cpu_percent_max: f64,
    mem_percent_avg: f64,
    mem_percent_max: f64,
    mem_used_mb_max: f64,
    mem_total_mb: f64,
    process_mem_mb_max: f64,
}

#[derive(Serialize)]
struct MetricsResponse {
    lang: &'static str,
    summary: MetricsSummary,
    history: Vec<MetricSample>,
}

struct MetricsHistory {
    start_time: Instant,
    history: Vec<MetricSample>,
}

struct AppState {
    total_requests: AtomicU64,
    mem_total_mb: f64,
    metrics: Mutex<MetricsHistory>,
}

type SharedState = Arc<AppState>;

fn round2(val: f64) -> f64 {
    (val * 100.0).round() / 100.0
}

fn count_primes(limit: u32) -> u64 {
    let mut count: u64 = 0;
    for num in 2..=limit {
        let mut is_prime = true;
        let sqrt = (num as f64).sqrt() as u32;
        for div in 2..=sqrt {
            if num % div == 0 {
                is_prime = false;
                break;
            }
        }
        if is_prime {
            count += 1;
        }
    }
    count
}

async fn compute_handler(
    State(state): State<SharedState>,
    Query(params): Query<ComputeParams>,
) -> Json<ComputeResponse> {
    let n = params.n.unwrap_or(50_000);

    let (primes, elapsed_ms) = tokio::task::spawn_blocking(move || {
        let start = Instant::now();
        let primes = count_primes(std::hint::black_box(n));
        let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
        (primes, elapsed_ms)
    })
    .await
    .unwrap();

    state.total_requests.fetch_add(1, Ordering::Relaxed);

    Json(ComputeResponse {
        lang: "rust",
        n,
        primes,
        elapsed_ms: (elapsed_ms * 1000.0).round() / 1000.0,
    })
}

async fn metrics_handler(State(state): State<SharedState>) -> Json<MetricsResponse> {
    let total_requests = state.total_requests.load(Ordering::Relaxed);
    let (duration_sec, samples) = {
        let guard = state.metrics.lock().unwrap();
        (
            round2(guard.start_time.elapsed().as_secs_f64()),
            guard.history.clone(),
        )
    };
    let count = samples.len();

    let (cpu_avg, cpu_max, mem_avg, mem_max, mem_used_max, proc_mem_max) = if count > 0 {
        let cpu_sum: f64 = samples.iter().map(|s| s.cpu_percent).sum();
        let cpu_max = samples.iter().map(|s| s.cpu_percent).fold(0.0, f64::max);
        let mem_sum: f64 = samples.iter().map(|s| s.mem_percent).sum();
        let mem_max = samples.iter().map(|s| s.mem_percent).fold(0.0, f64::max);
        let mem_used_max = samples.iter().map(|s| s.mem_used_mb).fold(0.0, f64::max);
        let proc_mem_max = samples.iter().map(|s| s.process_mem_mb).fold(0.0, f64::max);

        (
            round2(cpu_sum / count as f64),
            round2(cpu_max),
            round2(mem_sum / count as f64),
            round2(mem_max),
            round2(mem_used_max),
            round2(proc_mem_max),
        )
    } else {
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    };

    Json(MetricsResponse {
        lang: "rust",
        summary: MetricsSummary {
            duration_sec,
            samples_count: count,
            total_requests,
            cpu_percent_avg: cpu_avg,
            cpu_percent_max: cpu_max,
            mem_percent_avg: mem_avg,
            mem_percent_max: mem_max,
            mem_used_mb_max: mem_used_max,
            mem_total_mb: state.mem_total_mb,
            process_mem_mb_max: proc_mem_max,
        },
        history: samples,
    })
}

async fn reset_metrics_handler(State(state): State<SharedState>) -> Json<HealthResponse> {
    state.total_requests.store(0, Ordering::Relaxed);
    {
        let mut guard = state.metrics.lock().unwrap();
        guard.start_time = Instant::now();
        guard.history.clear();
    }

    Json(HealthResponse {
        status: "metrics_reset",
        lang: "rust",
    })
}

async fn health_handler() -> Json<HealthResponse> {
    Json(HealthResponse {
        status: "ok",
        lang: "rust",
    })
}

#[tokio::main]
async fn main() {
    let port: u16 = env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);

    let mut sys = System::new_all();
    sys.refresh_memory();
    sys.refresh_cpu();

    let total_vcpus = sys.cpus().len().max(1);
    let limit_cores: usize = env::var("LIMIT_CORES")
        .ok()
        .and_then(|c| c.parse().ok())
        .unwrap_or(total_vcpus)
        .max(1);

    let limit_ram_mb: f64 = env::var("LIMIT_RAM_MB")
        .ok()
        .and_then(|r| r.parse().ok())
        .unwrap_or(0.0);

    let vm_total_mb = round2(sys.total_memory() as f64 / (1024.0 * 1024.0));
    let mem_total_mb = if limit_ram_mb > 0.0 {
        limit_ram_mb
    } else {
        vm_total_mb
    };

    let state: SharedState = Arc::new(AppState {
        total_requests: AtomicU64::new(0),
        mem_total_mb,
        metrics: Mutex::new(MetricsHistory {
            start_time: Instant::now(),
            history: Vec::new(),
        }),
    });

    let monitor_state = Arc::clone(&state);
    tokio::spawn(async move {
        let pid = Pid::from_u32(std::process::id());
        let mut sys = System::new_all();
        sys.refresh_cpu();
        loop {
            tokio::time::sleep(Duration::from_secs(1)).await;
            sys.refresh_cpu();
            sys.refresh_memory();
            sys.refresh_process(pid);

            let cpus = sys.cpus();
            let active_count = limit_cores.min(cpus.len()).max(1);
            let cpu_sum: f32 = cpus.iter().take(active_count).map(|c| c.cpu_usage()).sum();
            let cpu_pct = (cpu_sum as f64) / (active_count as f64);

            let proc_mem_mb = sys
                .process(pid)
                .map(|p| p.memory() as f64 / (1024.0 * 1024.0))
                .unwrap_or(0.0);

            let (mem_used_mb, mem_pct) = if limit_ram_mb > 0.0 {
                let pct = ((proc_mem_mb / limit_ram_mb) * 100.0).min(100.0);
                (proc_mem_mb, pct)
            } else {
                let total_mem = sys.total_memory() as f64;
                let used_mem = sys.used_memory() as f64;
                let pct = if total_mem > 0.0 {
                    (used_mem / total_mem) * 100.0
                } else {
                    0.0
                };
                (used_mem / (1024.0 * 1024.0), pct)
            };

            let reqs = monitor_state.total_requests.load(Ordering::Relaxed);
            let mut guard = monitor_state.metrics.lock().unwrap();
            let elapsed_sec = round2(guard.start_time.elapsed().as_secs_f64());
            guard.history.push(MetricSample {
                elapsed_sec,
                cpu_percent: round2(cpu_pct),
                mem_percent: round2(mem_pct),
                mem_used_mb: round2(mem_used_mb),
                process_mem_mb: round2(proc_mem_mb),
                requests_so_far: reqs,
            });
        }
    });

    let app = Router::new()
        .route("/compute", get(compute_handler))
        .route("/metrics", get(metrics_handler))
        .route("/metrics/reset", get(reset_metrics_handler))
        .route("/health", get(health_handler))
        .with_state(state);

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    println!(
        "[Rust/Axum] Servidor rodando em http://{} (Cores={}, RAM={}MB)",
        addr, limit_cores, mem_total_mb
    );

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
