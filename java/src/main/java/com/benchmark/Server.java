package com.benchmark;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.sun.management.OperatingSystemMXBean;
import jakarta.annotation.PostConstruct;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.lang.management.ManagementFactory;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

@SpringBootApplication
@EnableScheduling
@RestController
public class Server {

    public record ComputeResponse(
            String lang,
            int n,
            long primes,
            @JsonProperty("elapsed_ms") double elapsedMs
    ) {}

    public record HealthResponse(
            String status,
            String lang,
            int cores,
            @JsonProperty("ram_mb") double ramMb
    ) {}

    public record MetricSample(
            @JsonProperty("elapsed_sec") double elapsedSec,
            @JsonProperty("cpu_percent") double cpuPercent,
            @JsonProperty("mem_percent") double memPercent,
            @JsonProperty("mem_used_mb") double memUsedMb,
            @JsonProperty("process_mem_mb") double processMemMb,
            @JsonProperty("requests_so_far") long requestsSoFar
    ) {}

    public record MetricsSummary(
            @JsonProperty("duration_sec") double durationSec,
            @JsonProperty("samples_count") int samplesCount,
            @JsonProperty("total_requests") long totalRequests,
            @JsonProperty("cpu_percent_avg") double cpuPercentAvg,
            @JsonProperty("cpu_percent_max") double cpuPercentMax,
            @JsonProperty("mem_percent_avg") double memPercentAvg,
            @JsonProperty("mem_percent_max") double memPercentMax,
            @JsonProperty("mem_used_mb_max") double memUsedMbMax,
            @JsonProperty("mem_total_mb") double memTotalMb,
            @JsonProperty("process_mem_mb_max") double processMemMbMax
    ) {}

    public record MetricsResponse(
            String lang,
            MetricsSummary summary,
            List<MetricSample> history
    ) {}

    private final Object metricsLock = new Object();
    private long startTimeNano = System.nanoTime();
    private final AtomicLong totalRequests = new AtomicLong(0);
    private final List<MetricSample> historySamples = new ArrayList<>();
    private final OperatingSystemMXBean osBean =
            ManagementFactory.getPlatformMXBean(OperatingSystemMXBean.class);

    private static final int TOTAL_VCPUS = Runtime.getRuntime().availableProcessors();
    private static final int LIMIT_CORES = parseIntOrDefault(System.getenv("LIMIT_CORES"), TOTAL_VCPUS);
    private static final double LIMIT_RAM_MB = Double.parseDouble(
            System.getenv().getOrDefault("LIMIT_RAM_MB", "0")
    );

    public static void main(String[] args) {
        int port = parseIntOrDefault(System.getenv("PORT"), 8080);
        int threads = parseIntOrDefault(
                System.getenv("THREADS"),
                Math.max(4, LIMIT_CORES * 4)
        );

        SpringApplication app = new SpringApplication(Server.class);
        app.setDefaultProperties(Map.of(
                "server.address", "0.0.0.0",
                "server.port", String.valueOf(port),
                "server.tomcat.threads.max", String.valueOf(threads),
                "server.tomcat.accept-count", "4096",
                "spring.threads.virtual.enabled", "true",
                "spring.main.banner-mode", "off",
                "logging.level.root", "WARN"
        ));
        app.run(args);

        System.out.printf("[Java/Spring Boot] Servidor rodando em http://0.0.0.0:%d (Cores=%d, RAM=%.0fMB)%n",
                port, LIMIT_CORES, LIMIT_RAM_MB);
    }

    @PostConstruct
    public void initCpuWarmup() {
        osBean.getCpuLoad();
        osBean.getProcessCpuLoad();
    }

    @Scheduled(fixedRate = 1000, initialDelay = 1000)
    public void collectSample() {
        double sysCpuLoad = osBean.getCpuLoad();
        double rawCpuPct = (sysCpuLoad < 0) ? 0.0 : sysCpuLoad * 100.0;
        double scale = (LIMIT_CORES > 0 && TOTAL_VCPUS > LIMIT_CORES)
                ? ((double) TOTAL_VCPUS / LIMIT_CORES)
                : 1.0;
        double cpuPct = round2(Math.min(100.0, rawCpuPct * scale));

        double procMemMb = round2(getProcessRssMb());
        long totalMemBytes = osBean.getTotalMemorySize();
        long freeMemBytes = osBean.getFreeMemorySize();
        long vmUsedBytes = Math.max(0L, totalMemBytes - freeMemBytes);
        double vmUsedMb = vmUsedBytes / (1024.0 * 1024.0);

        double memUsedMb;
        double memPct;
        if (LIMIT_RAM_MB > 0) {
            memUsedMb = procMemMb;
            memPct = round2(Math.min(100.0, (procMemMb / LIMIT_RAM_MB) * 100.0));
        } else {
            memUsedMb = round2(vmUsedMb);
            memPct = round2((totalMemBytes > 0) ? ((double) vmUsedBytes / totalMemBytes) * 100.0 : 0.0);
        }

        synchronized (metricsLock) {
            double elapsedSec = round2((System.nanoTime() - startTimeNano) / 1_000_000_000.0);
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

    @GetMapping("/health")
    public HealthResponse health() {
        return new HealthResponse("ok", "java", LIMIT_CORES, LIMIT_RAM_MB);
    }

    @GetMapping("/compute")
    public ComputeResponse compute(@RequestParam(name = "n", defaultValue = "50000") int n) {
        long startNano = System.nanoTime();
        long primeCount = countPrimes(n);
        double elapsedMs = round3((System.nanoTime() - startNano) / 1_000_000.0);

        totalRequests.incrementAndGet();

        return new ComputeResponse("java", n, primeCount, elapsedMs);
    }

    @GetMapping("/metrics/reset")
    public Map<String, String> resetMetrics() {
        synchronized (metricsLock) {
            startTimeNano = System.nanoTime();
            totalRequests.set(0);
            historySamples.clear();
        }
        return Map.of("status", "metrics_reset", "lang", "java");
    }

    @GetMapping("/metrics")
    public MetricsResponse metrics() {
        List<MetricSample> snapshot;
        double durationSec;
        long reqs = totalRequests.get();
        double memTotalMb = round2((LIMIT_RAM_MB > 0)
                ? LIMIT_RAM_MB
                : (osBean.getTotalMemorySize() / (1024.0 * 1024.0)));

        synchronized (metricsLock) {
            durationSec = round2((System.nanoTime() - startTimeNano) / 1_000_000_000.0);
            snapshot = new ArrayList<>(historySamples);
        }

        int count = snapshot.size();
        double cpuAvg = 0, cpuMax = 0, memAvg = 0, memMax = 0, memUsedMax = 0, procMemMax = 0;

        if (count > 0) {
            double cpuSum = 0, memSum = 0;
            for (MetricSample s : snapshot) {
                cpuSum += s.cpuPercent();
                memSum += s.memPercent();
                if (s.cpuPercent() > cpuMax) cpuMax = s.cpuPercent();
                if (s.memPercent() > memMax) memMax = s.memPercent();
                if (s.memUsedMb() > memUsedMax) memUsedMax = s.memUsedMb();
                if (s.processMemMb() > procMemMax) procMemMax = s.processMemMb();
            }
            cpuAvg = round2(cpuSum / count);
            memAvg = round2(memSum / count);
        }

        MetricsSummary summary = new MetricsSummary(
                durationSec,
                count,
                reqs,
                cpuAvg,
                round2(cpuMax),
                memAvg,
                round2(memMax),
                round2(memUsedMax),
                memTotalMb,
                round2(procMemMax)
        );

        return new MetricsResponse("java", summary, Collections.unmodifiableList(snapshot));
    }

    private static double getProcessRssMb() {
        try {
            for (String line : Files.readAllLines(Paths.get("/proc/self/status"))) {
                if (line.startsWith("VmRSS:")) {
                    String[] parts = line.replaceAll("[^0-9]", " ").trim().split("\\s+");
                    if (parts.length > 0 && !parts[0].isEmpty()) {
                        return Double.parseDouble(parts[0]) / 1024.0;
                    }
                }
            }
        } catch (Exception ignored) {
        }
        Runtime rt = Runtime.getRuntime();
        return (rt.totalMemory() - rt.freeMemory()) / (1024.0 * 1024.0);
    }

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

    private static double round2(double val) {
        return Math.round(val * 100.0) / 100.0;
    }

    private static double round3(double val) {
        return Math.round(val * 1000.0) / 1000.0;
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
