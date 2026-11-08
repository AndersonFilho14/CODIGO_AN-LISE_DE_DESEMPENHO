use axum::{
    extract::Query,
    routing::get,
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{env, net::SocketAddr, time::Instant};

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

async fn compute_handler(Query(params): Query<ComputeParams>) -> Json<ComputeResponse> {
    let n = params.n.unwrap_or(50_000);

    // Executa em thread pool de bloqueio para nao travar o event loop async sob carga de CPU
    let (primes, elapsed_ms) = tokio::task::spawn_blocking(move || {
        let start = Instant::now();
        let primes = count_primes(std::hint::black_box(n));
        let elapsed_ms = start.elapsed().as_secs_f64() * 1000.0;
        (primes, elapsed_ms)
    })
    .await
    .unwrap();

    Json(ComputeResponse {
        lang: "rust",
        n,
        primes,
        elapsed_ms,
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

    let app = Router::new()
        .route("/compute", get(compute_handler))
        .route("/health", get(health_handler));

    let addr = SocketAddr::from(([0, 0, 0, 0], port));
    println!("[Rust] Servidor rodando em http://{}", addr);

    let listener = tokio::net::TcpListener::bind(addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
