<?php
/**
 * Stuurt /api naar Kasflow op 127.0.0.1:8010.
 * Staat de app niet aan, dan probeert dit script plesk/start.sh (geen root).
 */
set_time_limit(90);

$path = isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/';
$pathname = parse_url($path, PHP_URL_PATH);
if (!is_string($pathname) || strpos($pathname, '/api/') !== 0) {
    header('Content-Type: application/json; charset=utf-8', true, 404);
    echo '{"detail":"Not Found"}';
    exit;
}

$backend = getenv('KASFLOW_BACKEND') ? getenv('KASFLOW_BACKEND') : 'http://127.0.0.1:8010';
$root = __DIR__;

function kasflow_port_open()
{
    $fp = @fsockopen('127.0.0.1', 8010, $errno, $errstr, 0.4);
    if (is_resource($fp)) {
        fclose($fp);
        return true;
    }
    return false;
}

function kasflow_can_exec()
{
    $disabled = array_map('trim', explode(',', (string) ini_get('disable_functions')));
    return !in_array('exec', $disabled, true);
}

function kasflow_ensure_running($root)
{
    if (kasflow_port_open()) {
        return true;
    }
    if (!kasflow_can_exec()) {
        return false;
    }
    $start = $root . '/plesk/start.sh';
    $deploy = $root . '/plesk/deploy.sh';
    $log = $root . '/backend/logs';
    if (!is_dir($log)) {
        @mkdir($log, 0770, true);
    }
    $script = is_file($root . '/venv/bin/gunicorn') ? $start : $deploy;
    if (!is_file($script)) {
        return false;
    }
    $cmd = 'bash ' . escapeshellarg($script) . ' >> ' . escapeshellarg($log . '/autostart.log') . ' 2>&1';
    exec($cmd);
    for ($i = 0; $i < 20; $i++) {
        if (kasflow_port_open()) {
            return true;
        }
        usleep(400000);
    }
    return kasflow_port_open();
}

if (!kasflow_ensure_running($root)) {
    header('Content-Type: application/json; charset=utf-8', true, 503);
    echo json_encode(array(
        'detail' => 'Kasflow is na Git-deploy niet gestart. In Plesk bij Git: Extra implementatie-acties = bash plesk/deploy.sh. Daarna de site opnieuw publiceren.',
    ));
    exit;
}

$url = rtrim($backend, '/') . $path;
$method = isset($_SERVER['REQUEST_METHOD']) ? $_SERVER['REQUEST_METHOD'] : 'GET';
$https = !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off';
$host = isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : 'localhost';

$headers = array();
foreach ($_SERVER as $key => $value) {
    if (strpos($key, 'HTTP_') !== 0) {
        continue;
    }
    $name = strtolower(str_replace('_', '-', substr($key, 5)));
    if (in_array($name, array('host', 'connection', 'content-length', 'transfer-encoding'), true)) {
        continue;
    }
    $headers[] = $name . ': ' . $value;
}
if (!empty($_SERVER['CONTENT_TYPE'])) {
    $headers[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE'];
}
$headers[] = 'Host: ' . $host;
$headers[] = 'X-Forwarded-Host: ' . $host;
$headers[] = 'X-Forwarded-Proto: ' . ($https ? 'https' : 'http');
$headers[] = 'X-Forwarded-For: ' . (isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '');

$body = in_array($method, array('GET', 'HEAD'), true) ? null : file_get_contents('php://input');

$curl = curl_init($url);
$options = array(
    CURLOPT_CUSTOMREQUEST => $method,
    CURLOPT_HTTPHEADER => $headers,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_HEADER => true,
    CURLOPT_TIMEOUT => 120,
    CURLOPT_HTTP_VERSION => CURL_HTTP_VERSION_1_1,
);
if ($body !== null && $body !== false && $body !== '') {
    $options[CURLOPT_POSTFIELDS] = $body;
}
curl_setopt_array($curl, $options);
$raw = curl_exec($curl);
$errno = curl_errno($curl);
$headerSize = (int) curl_getinfo($curl, CURLINFO_HEADER_SIZE);
$status = (int) curl_getinfo($curl, CURLINFO_HTTP_CODE);
curl_close($curl);

if ($raw === false || $errno) {
    header('Content-Type: application/json; charset=utf-8', true, 503);
    echo json_encode(array(
        'detail' => 'Kasflow is na Git-deploy niet gestart. In Plesk bij Git: Extra implementatie-acties = bash plesk/deploy.sh. Daarna de site opnieuw publiceren.',
    ));
    exit;
}

$head = substr($raw, 0, $headerSize);
$payload = substr($raw, $headerSize);
http_response_code($status ? $status : 502);
foreach (explode("\r\n", $head) as $line) {
    if ($line === '' || strncmp($line, 'HTTP/', 5) === 0) {
        continue;
    }
    $lower = strtolower($line);
    if (strpos($lower, 'transfer-encoding:') === 0 || strpos($lower, 'connection:') === 0) {
        continue;
    }
    header($line, false);
}
echo $payload;
