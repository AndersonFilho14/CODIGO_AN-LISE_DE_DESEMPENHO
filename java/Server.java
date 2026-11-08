import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.Executors;

public class Server {

    public static void main(String[] args) throws IOException {
        int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8080"));
        int threads = Integer.parseInt(System.getenv().getOrDefault(
                "THREADS",
                String.valueOf(Runtime.getRuntime().availableProcessors() * 2)
        ));

        // Backlog de 4096 conexoes pendentes para suportar picos do JMeter
        HttpServer server = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 4096);

        server.createContext("/compute", new ComputeHandler());
        server.createContext("/health", exchange -> {
            byte[] response = "{\"status\":\"ok\",\"lang\":\"java\"}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(response);
            }
        });

        // Pool fixo de threads nativas (ideal para tarefas CPU-bound)
        server.setExecutor(Executors.newFixedThreadPool(threads));
        server.start();

        System.out.printf("[Java] Servidor rodando em http://0.0.0.0:%d (Threads: %d)%n", port, threads);
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

            String json = String.format(
                    "{\"lang\":\"java\",\"n\":%d,\"primes\":%d,\"elapsed_ms\":%.3f}",
                    n, primeCount, elapsedMs
            );
            byte[] response = json.getBytes(StandardCharsets.UTF_8);

            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, response.length);
            try (OutputStream os = exchange.getResponseBody()) {
                os.write(response);
            }
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
