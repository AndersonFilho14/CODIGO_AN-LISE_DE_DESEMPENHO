import com.sun.management.OperatingSystemMXBean;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;

public class Server {

    static class MetricSample {
        final double elapsedSec;
        final double cpuPercent;
        final double memPercent;
        final double memUsedMb;
        final double processMemMb;
        final long requestsSoFar;

        MetricSample(double elapsedSec, double cpuPercent, double memPercent,
                     double memUsedMb, double processMemMb, long requestsSoFar) {
            this.elapsedSec = elapsedSec;
            this.cpuPercent = cpuPercent;
            this.memPercent = memPercent;
            this.memUsedMb = memUsedMb;
            this.processMemMb = processMemMb;
            this.requestsSoFar = requestsSoFar;
        }
    }

    private static final Object METRICS_LOCK = new Object();
    private static long startTimeNano = System.nanoTime();
    private static final AtomicLong totalRequests = new AtomicLong(0);
    private static final List<MetricSample> historySamples = new ArrayList<>();
    private static final OperatingSystemMXBean osBean =
            ManagementFactory.getPlatformMXBean(OperatingSystemMXBean.class);

    public static void main(String[] args) throws IOException {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
        int threads = Integer.parseInt(System.getenv().getOrDefault(
                "THREADS",
                String.valueOf(Runtime.getRuntime().availableProcessors() * 2)
        ));

        // Inicia o monitoramento em background a cada 1 segundo (equivalente ao psutil)
        ScheduledExecutorService monitor = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "metrics-monitor");
            t.setDaemon(true);
            return t;
        });
        osBean.getCpuLoad(); // Inicializa leitura de CPU
        monitor.scheduleAtFixedRate(Server::collectSample, 1, 1, TimeUnit.SECONDS);

        HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 4096);

        server.createContext("/compute", new ComputeHandler());
        server.createContext("/metrics", new MetricsHandler());
        server.createContext("/metrics/reset", new ResetMetricsHandler());
        server.createContext("/health", exchange -> {
            sendJson(exchange, 200, "{\"status\":\"ok\",\"lang\":\"java\"}");
        });

        server.setExecutor(Executors.newFixedThreadPool(threads));
        server.start();

        System.out.printf("[Java] Servidor rodando em http://0.0.0.0:%d (Threads: %d)%n", port, threads);
        System.out.printf("  -> Carga CPU : http://0.0.0.0:%d/compute?n=50000%n", port);
        System.out.printf("  -> Metricas  : http://0.0.0.0:%d/metrics%n", port);
        System.out.printf("  -> Resetar   : http://0.0.0.0:%d/metrics/reset%n", port);
    }

    private static void collectSample() {
        double cpuLoad = osBean.getCpuLoad();
        double cpuPct = (cpuLoad < 0) ? 0.0 : cpuLoad * 100.0;

        long totalMemBytes = osBean.getTotalMemorySize();
        long freeMemBytes = osBean.getFreeMemorySize();
        long usedMemBytes = Math.max(0L, totalMemBytes - freeMemBytes);

        double memUsedMb = usedMemBytes / (1024.0 * 1024.0);
        double memPct = (totalMemBytes > 0) ? ((double) usedMemBytes / totalMemBytes) * 100.0 : 0.0;

        Runtime rt = Runtime.getRuntime();
        double procMemMb = (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);

        synchronized (METRICS_LOCK) {
            double elapsedSec = (System.nanoTime() - startTimeNano) / 1_000_000_000.0;
            historySamples.add(new MetricSample(
                     elapsedSec,
                     cpuPct,
                     memPct,
                     memUsedMb,
                     procMemMb,
                     totalRequests.get()
            ));
        }
    }

    static class ComputeHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (!"GET".equalsIgnoreCase(exchange.getRequestMethod())) {
                exchange.sendResponseHeaders(405, -1);
                return;
            }

            Map<String, String> params = parseQuery(exchange.getRequestURI().getRawQuery());
            int n = parseIntOrDefault(params.get("n"), 50000);

            long startNano = System.nanoTime();
            long primeCount = countPrimes(n);
            double elapsedMs = (System.nanoTime() - startNano) / 1_000_000.0;

            totalRequests.incrementAndGet();

            String json = String.format(Locale.US,
                    "{\"lang\":\"java\",\"n\":%d,\"primes\":%d,\"elapsed_ms\":%.3f}",
                    n, primeCount, elapsedMs
            );
            sendJson(exchange, 200, json);
        }
    }

    static class MetricsHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            List<MetricSample> snapshot;
            double durationSec;
            long reqs = totalRequests.get();
            double memTotalMb = osBean.getTotalMemorySize() / (1024.0 * 1024.0);

            synchronized (METRICS_LOCK) {
                durationSec = (System.nanoTime() - startTimeNano) / 1_000_000_000.0;
                snapshot = new ArrayList<>(historySamples);
            }

            int count = snapshot.size();
            double cpuAvg = 0, cpuMax = 0, memAvg = 0, memMax = 0, memUsedMax = 0, procMemMax = 0;

            if (count > 0) {
                double cpuSum = 0, memSum = 0;
                for (MetricSample s : snapshot) {
                    cpuSum += s.cpuPercent;
                    memSum += s.memPercent;
                    if (s.cpuPercent > cpuMax) cpuMax = s.cpuPercent;
                    if (s.memPercent > memMax) memMax = s.memPercent;
                    if (s.memUsedMb > memUsedMax) memUsedMax = s.memUsedMb;
                    if (s.processMemMb > procMemMax) procMemMax = s.processMemMb;
                }
                cpuAvg = cpuSum / count;
                memAvg = memSum / count;
            }

            StringBuilder sb = new StringBuilder(1024 + count * 128);
            sb.append("{\"lang\":\"java\",\"summary\":");
            sb.append(String.format(Locale.US,
                    "{\"duration_sec\":%.2f,\"samples_count\":%d,\"total_requests\":%d," +
                    "\"cpu_percent_avg\":%.2f,\"cpu_percent_max\":%.2f," +
                    "\"mem_percent_avg\":%.2f,\"mem_percent_max\":%.2f," +
                    "\"mem_used_mb_max\":%.2f,\"mem_total_mb\":%.2f,\"process_mem_mb_max\":%.2f},",
                    durationSec, count, reqs,
                    cpuAvg, cpuMax, memAvg, memMax, memUsedMax, memTotalMb, procMemMax
            ));

            sb.append("\"history\":[");
            for (int i = 0; i < count; i++) {
                if (i > 0) sb.append(',');
                MetricSample s = snapshot.get(i);
                sb.append(String.format(Locale.US,
                        "{\"elapsed_sec\":%.2f,\"cpu_percent\":%.2f,\"mem_percent\":%.2f," +
                        "\"mem_used_mb\":%.2f,\"process_mem_mb\":%.2f,\"requests_so_far\":%d}",
                        s.elapsedSec, s.cpuPercent, s.memPercent,
                        s.memUsedMb, s.processMemMb, s.requestsSoFar
                ));
            }
            sb.append("]}");

            sendJson(exchange, 200, sb.toString());
        }
    }

    static class ResetMetricsHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            synchronized (METRICS_LOCK) {
                startTimeNano = System.nanoTime();
                totalRequests.set(0);
                historySamples.clear();
            }
            sendJson(exchange, 200, "{\"status\":\"metrics_reset\",\"lang\":\"java\"}");
        }
    }

    private static void sendJson(HttpExchange exchange, int status, String json) throws IOException {
        byte[] response = json.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, response.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(response);
        }
    }

    /**
     * Carga intensiva de CPU (CPU-bound):
     * Verifica numeros primos ate N por divisao direta O(N * sqrt(N))
     * para estressar ALU e pipeline da CPU de forma identica nas 3 linguagens.
     */
    private static long countPrimes(int limit) {
        long count = 0;
        for (int num = 2; num <= limit; num++) {
            boolean isPrime = true;
            int sqrt = (int) Math.sqrt(num);
            for (int div = 2; div <= sqrt; div++) {
                if (num % div == 0) {
                    isPrime = false;
                    break;
                }
            }
            if (isPrime) {
                count++;
            }
        }
        return count;
    }

    private static Map<String, String> parseQuery(String query) {
        Map<String, String> result = new HashMap<>();
        if (query == null || query.isEmpty()) {
            return result;
        }
        for (String param : query.split("&")) {
            String[] pair = param.split("=", 2);
            if (pair.length == 2) {
                result.put(pair[0], pair[1]);
            }
        }
        return result;
    }

    private static int parseIntOrDefault(String value, int defaultValue) {
        if (value == null) return defaultValue;
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException e) {
            return defaultValue;
        }
    }
}
