<?php
/**
 * /api op Plesk: voer FastAPI uit als Python-CGI vanuit PHP.
 * LiteSpeed verbiedt .cgi (403). Poort 8010 is niet nodig.
 */
set_time_limit(180);

$path = isset($_SERVER['REQUEST_URI']) ? $_SERVER['REQUEST_URI'] : '/';
$pathname = parse_url($path, PHP_URL_PATH);
if (!is_string($pathname) || strpos($pathname, '/api/') !== 0) {
    header('Content-Type: application/json; charset=utf-8', true, 404);
    echo '{"detail":"Not Found"}';
    exit;
}

$root = __DIR__;
$method = isset($_SERVER['REQUEST_METHOD']) ? $_SERVER['REQUEST_METHOD'] : 'GET';
$body = in_array($method, array('GET', 'HEAD'), true) ? '' : (string) file_get_contents('php://input');

function kasflow_disabled()
{
    return array_map('trim', explode(',', (string) ini_get('disable_functions')));
}

function kasflow_fail($message, $status = 503)
{
    header('Content-Type: application/json; charset=utf-8', true, $status);
    echo json_encode(array('detail' => $message), JSON_UNESCAPED_UNICODE);
    exit;
}

function kasflow_proc($command, $cwd, $env, $stdin = '')
{
    if (in_array('proc_open', kasflow_disabled(), true)) {
        return array(false, '', 'proc_open is uitgeschakeld');
    }
    $spec = array(
        0 => array('pipe', 'r'),
        1 => array('pipe', 'w'),
        2 => array('pipe', 'w'),
    );
    $proc = proc_open($command, $spec, $pipes, $cwd, $env);
    if (!is_resource($proc)) {
        return array(false, '', 'proc_open gaf geen proces');
    }
    if ($stdin !== '') {
        fwrite($pipes[0], $stdin);
    }
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($proc);
    return array($code === 0, $out, $err);
}

function kasflow_bootstrap($root)
{
    $python = $root . '/venv/bin/python';
    if (is_executable($python) && is_file($root . '/.env')) {
        return $python;
    }
    $script = $root . '/plesk/bootstrap.sh';
    if (!is_file($script)) {
        kasflow_fail('bootstrap.sh ontbreekt in de documentroot. Git-deploy opnieuw uitvoeren.');
    }
    list($ok, $out, $err) = kasflow_proc(array('/bin/bash', $script), $root, array(
        'PATH' => '/opt/plesk/python/3.11/bin:/usr/local/bin:/usr/bin:/bin',
        'HOME' => $root,
        'PWD' => $root,
    ));
    $python = $root . '/venv/bin/python';
    if (!is_executable($python)) {
        $log = trim($err . "\n" . $out);
        kasflow_fail('Python kon niet worden geïnstalleerd. ' . ($log ? $log : 'Draai in SSH: bash plesk/bootstrap.sh'));
    }
    return $python;
}

$python = kasflow_bootstrap($root);
$env = array(
    'PATH' => '/usr/local/bin:/usr/bin:/bin',
    'HOME' => $root,
    'PWD' => $root,
    'PYTHONUNBUFFERED' => '1',
    'PYTHONDONTWRITEBYTECODE' => '1',
    'REQUEST_METHOD' => $method,
    'REQUEST_URI' => $path,
    'SCRIPT_NAME' => '',
    'PATH_INFO' => $pathname,
    'QUERY_STRING' => isset($_SERVER['QUERY_STRING']) ? $_SERVER['QUERY_STRING'] : '',
    'CONTENT_TYPE' => isset($_SERVER['CONTENT_TYPE']) ? $_SERVER['CONTENT_TYPE'] : '',
    'CONTENT_LENGTH' => (string) strlen($body),
    'SERVER_NAME' => isset($_SERVER['SERVER_NAME']) ? $_SERVER['SERVER_NAME'] : 'localhost',
    'SERVER_PORT' => isset($_SERVER['SERVER_PORT']) ? $_SERVER['SERVER_PORT'] : '443',
    'SERVER_PROTOCOL' => 'HTTP/1.1',
    'REMOTE_ADDR' => isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '',
    'HTTPS' => 'on',
    'HTTP_HOST' => isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : '',
    'HTTP_COOKIE' => isset($_SERVER['HTTP_COOKIE']) ? $_SERVER['HTTP_COOKIE'] : '',
    'HTTP_ORIGIN' => isset($_SERVER['HTTP_ORIGIN']) ? $_SERVER['HTTP_ORIGIN'] : '',
    'HTTP_ACCEPT' => isset($_SERVER['HTTP_ACCEPT']) ? $_SERVER['HTTP_ACCEPT'] : 'application/json',
    'HTTP_USER_AGENT' => isset($_SERVER['HTTP_USER_AGENT']) ? $_SERVER['HTTP_USER_AGENT'] : 'Kasflow',
    'HTTP_X_FORWARDED_PROTO' => 'https',
    'HTTP_X_FORWARDED_FOR' => isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '',
);
foreach ($_SERVER as $key => $value) {
    if (strpos($key, 'HTTP_') === 0 && is_string($value) && !isset($env[$key])) {
        $env[$key] = $value;
    }
}

list($ok, $raw, $err) = kasflow_proc(
    array($python, $root . '/plesk/cgi_app.py'),
    $root,
    $env,
    $body
);

if ($raw === '' || $raw === false) {
    $log = trim($err);
    kasflow_fail($log ? $log : 'Python startte niet. Controleer backend/logs/cgi.log');
}

$headerSize = strpos($raw, "\r\n\r\n");
$sep = "\r\n\r\n";
if ($headerSize === false) {
    $headerSize = strpos($raw, "\n\n");
    $sep = "\n\n";
}
if ($headerSize === false) {
    header('Content-Type: application/json; charset=utf-8');
    echo $raw;
    exit;
}

$head = substr($raw, 0, $headerSize);
$payload = substr($raw, $headerSize + strlen($sep));
$status = 200;
foreach (preg_split("/\r\n|\n/", $head) as $line) {
    if ($line === '') {
        continue;
    }
    if (preg_match('/^Status:\s+(\d+)/i', $line, $match)) {
        $status = (int) $match[1];
        continue;
    }
    if (preg_match('/^HTTP\/\d\.\d\s+(\d+)/', $line, $match)) {
        $status = (int) $match[1];
        continue;
    }
    $lower = strtolower($line);
    if (strpos($lower, 'transfer-encoding:') === 0 || strpos($lower, 'connection:') === 0) {
        continue;
    }
    header($line, false);
}
http_response_code($status);
echo $payload;
