use axum::{
    extract::{Query, State},
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{
    env,
    net::SocketAddr,
    sync::{Arc, Mutex},
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

struct MetricsState {
    start_time: Instant,
    total_requests: u64,
    mem_total_mb: f64,
    history: Vec<MetricSample>,
}

type SharedState = Arc<Mutex<MetricsState>>;

fn round2(val: f64) -> f64 {
    (val * 100.0).round() / 100.0
}

/// Carga intensiva de CPU (CPU-bound):
/// Verifica numeros primos ate N por divisao direta O(N * sqrt(N))
/// exatamente com a mesma logica de Java e Python.
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

    {
        let mut guard = state.lock().unwrap();
        guard.total_requests += 1;
    }

    Json(ComputeResponse {
        lang: "rust",
        n,
        primes,
        elapsed_ms: (elapsed_ms * 1000.0).round() / 1000.0,
    })
}

async fn metrics_handler(State(state): State<SharedState>) -> Json<MetricsResponse> {
    let guard = state.lock().unwrap();
    let duration_sec = round2(guard.start_time.elapsed().as_secs_f64());
    let samples = guard.history.clone();
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
            total_requests: guard.total_requests,
            cpu_percent_avg: cpu_avg,
            cpu_percent_max: cpu_max,
            mem_percent_avg: mem_avg,
            mem_percent_max: mem_max,
            mem_used_mb_max: mem_used_max,
            mem_total_mb: guard.mem_total_mb,
            process_mem_mb_max: proc_mem_max,
        },
        history: samples,
    })
}

async fn reset_metrics_handler(State(state): State<SharedState>) -> Json<HealthResponse> {
    let mut guard = state.lock().unwrap();
    guard.start_time = Instant::now();
    guard.total_requests = 0;
    guard.history.clear();

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
    let mem_total_mb = round2(sys.total_memory() as f64 / (1024.0 * 1024.0));

    let state: SharedState = Arc::new(Mutex::new(MetricsState {
        start_time: Instant::now(),
        total_requests: 0,
        mem_total_mb,
        history: Vec::new(),
    }));

    // Thread em background (equivalente ao psutil) coletando CPU e RAM a cada 1 segundo
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

            let cpu_pct = sys.global_cpu_info().cpu_usage() as f64;
            let total_mem = sys.total_memory() as f64;
            let used_mem = sys.used_memory() as f64;
            let mem_pct = if total_mem > 0.0 {
                (used_mem / total_mem) * 100.0
            } else {
                0.0
            };
            let mem_used_mb = used_mem / (1024.0 * 1024.0);
            let proc_mem_mb = sys
                .process(pid)
                .map(|p| p.memory() as f64 / (1024.0 * 1024.0))
                .unwrap_or(0.0);

            let mut guard = monitor_state.lock().unwrap();
            let elapsed_sec = round2(guard.start_time.elapsed().as_secs_f64());
            let reqs = guard.total_requests;
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
    println!("[Rust] Servidor rodando em http://{}", addr);
    println!("  -> Carga CPU : http://{}/compute?n=50000", addr);
    println!("  -> Metricas  : http://{}/metrics", addr);
    println!("  -> Resetar   : http://{}/metrics/reset", addr);

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
