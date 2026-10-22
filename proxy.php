<?php
/**
 * /api op Plesk/LiteSpeed: PHP start Python en geeft het antwoord door.
 *
 * De hosting laat geen langdurig proces of systemd toe, dus FastAPI draait per
 * verzoek als CGI-proces. Dit bestand zorgt dat de venv en de packages er zijn
 * voordat de app start: ontbreken ze, dan draait het eenmalig plesk/bootstrap.sh
 * en probeert het opnieuw.
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

function kasflow_fail($message, $status = 503)
{
    header('Content-Type: application/json; charset=utf-8', true, $status);
    echo json_encode(array('detail' => $message), JSON_UNESCAPED_UNICODE);
    exit;
}

function kasflow_tail($text, $lines = 3)
{
    $parts = preg_split("/\r\n|\n|\r/", trim((string) $text));
    $parts = array_values(array_filter($parts, function ($line) {
        return trim($line) !== '';
    }));
    $tail = implode(' | ', array_slice($parts, -$lines));
    return strlen($tail) > 400 ? substr($tail, 0, 400) . '…' : $tail;
}

function kasflow_proc($command, $cwd, $env, $stdin = '', $timeout = 120)
{
    if (!function_exists('proc_open')) {
        return array(false, '', 'proc_open bestaat niet in deze PHP');
    }
    $disabled = array_map('trim', explode(',', (string) ini_get('disable_functions')));
    if (in_array('proc_open', $disabled, true)) {
        return array(false, '', 'proc_open is uitgeschakeld');
    }
    $spec = array(
        0 => array('pipe', 'r'),
        1 => array('pipe', 'w'),
        2 => array('pipe', 'w'),
    );
    $proc = @proc_open($command, $spec, $pipes, $cwd, $env);
    if (!is_resource($proc)) {
        return array(false, '', 'proc_open kon het proces niet starten');
    }
    if ($stdin !== '') {
        fwrite($pipes[0], $stdin);
    }
    fclose($pipes[0]);

    $deadline = time() + $timeout;
    stream_set_blocking($pipes[1], false);
    stream_set_blocking($pipes[2], false);
    $out = '';
    $err = '';
    while (true) {
        $out .= (string) stream_get_contents($pipes[1]);
        $err .= (string) stream_get_contents($pipes[2]);
        $status = proc_get_status($proc);
        if (!$status['running']) {
            break;
        }
        if (time() > $deadline) {
            proc_terminate($proc, 9);
            $err .= "\nproces afgebroken na {$timeout}s";
            break;
        }
        usleep(10000);
    }
    $out .= (string) stream_get_contents($pipes[1]);
    $err .= (string) stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    proc_close($proc);

    return array(trim($err) === '', $out, $err);
}

/** Pad naar een Python uit de venv, of null als de venv niet bruikbaar is. */
function kasflow_venv_python($root)
{
    // De laatste variant is voor ontwikkelen op Windows; op de server telt bin/.
    foreach (array('/venv/bin/python', '/venv/bin/python3', '/venv/Scripts/python.exe') as $rel) {
        $python = $root . $rel;
        if (is_file($python) && is_executable($python)) {
            return $python;
        }
    }
    return null;
}

/** Vingerafdruk van beide eisenlijsten, zodat een wijziging opvalt. */
function kasflow_requirements_hash($root)
{
    $parts = '';
    foreach (array('/backend/requirements.txt', '/backend/requirements-legacy.txt') as $rel) {
        $file = $root . $rel;
        $parts .= is_file($file) ? md5_file($file) : '-';
    }
    return md5($parts);
}

function kasflow_deps_state($root)
{
    $marker = $root . '/backend/logs/deps.ok';
    if (!is_file($marker)) {
        return 'onbekend';
    }
    return trim((string) file_get_contents($marker)) === kasflow_requirements_hash($root) ? 'ok' : 'verouderd';
}

function kasflow_python_version($python, $root)
{
    list(, $out, $err) = kasflow_proc(array($python, '-V'), $root, kasflow_base_env($root), '', 20);
    $version = trim($out . ' ' . $err);
    return $version !== '' ? $version : 'onbekende Python';
}

function kasflow_base_env($root)
{
    return array(
        'PATH' => '/opt/plesk/python/3.13/bin:/opt/plesk/python/3.12/bin:/opt/plesk/python/3.11/bin'
            . ':/usr/local/bin:/usr/bin:/bin',
        'HOME' => $root,
        'PWD' => $root,
        'LANG' => 'C.UTF-8',
        'PYTHONUNBUFFERED' => '1',
        'PYTHONDONTWRITEBYTECODE' => '1',
        'PIP_DISABLE_PIP_VERSION_CHECK' => '1',
    );
}

/** Zorgt dat venv en packages klaar zijn; geeft een foutmelding of null. */
function kasflow_ensure_ready($root)
{
    $python = kasflow_venv_python($root);
    $has_env = is_file($root . '/.env');
    if ($python !== null && $has_env && kasflow_deps_state($root) === 'ok') {
        return null;
    }

    $script = $root . '/plesk/bootstrap.sh';
    if (!is_file($script)) {
        return 'plesk/bootstrap.sh ontbreekt. Laat Plesk de nieuwste versie van de repo uitpakken.';
    }

    list(, $out, $err) = kasflow_proc(
        array('/bin/bash', $script),
        $root,
        kasflow_base_env($root),
        '',
        170
    );
    $log = kasflow_tail($err . ' ' . $out, 3);

    $python = kasflow_venv_python($root);
    if ($python === null) {
        return 'Python kon niet worden klaargezet. ' . $log;
    }

    list($ok, $probe, $probeErr) = kasflow_proc(
        array($python, '-c', 'import fastapi, a2wsgi, dotenv, sqlalchemy, reportlab, httpx'),
        $root,
        kasflow_base_env($root),
        '',
        60
    );
    if (!$ok) {
        return 'De Python-packages ontbreken nog (' . kasflow_python_version($python, $root) . '). '
            . $log . ' ' . kasflow_tail($probeErr . ' ' . $probe, 1);
    }

    $marker = $root . '/backend/logs/deps.ok';
    if (is_dir(dirname($marker))) {
        @file_put_contents($marker, kasflow_requirements_hash($root));
    }

    return null;
}

$problem = kasflow_ensure_ready($root);
if ($problem !== null) {
    kasflow_fail($problem);
}

$env = kasflow_base_env($root) + array(
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
    'REMOTE_HOST' => isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '',
    'HTTPS' => 'on',
    'HTTP_HOST' => isset($_SERVER['HTTP_HOST']) ? $_SERVER['HTTP_HOST'] : '',
    'HTTP_COOKIE' => isset($_SERVER['HTTP_COOKIE']) ? $_SERVER['HTTP_COOKIE'] : '',
    'HTTP_ORIGIN' => isset($_SERVER['HTTP_ORIGIN']) ? $_SERVER['HTTP_ORIGIN'] : '',
    'HTTP_ACCEPT' => isset($_SERVER['HTTP_ACCEPT']) ? $_SERVER['HTTP_ACCEPT'] : 'application/json',
    'HTTP_ACCEPT_LANGUAGE' => isset($_SERVER['HTTP_ACCEPT_LANGUAGE']) ? $_SERVER['HTTP_ACCEPT_LANGUAGE'] : '',
    'HTTP_USER_AGENT' => isset($_SERVER['HTTP_USER_AGENT']) ? $_SERVER['HTTP_USER_AGENT'] : 'Kasflow',
    'HTTP_REFERER' => isset($_SERVER['HTTP_REFERER']) ? $_SERVER['HTTP_REFERER'] : '',
    'HTTP_X_FORWARDED_PROTO' => 'https',
    'HTTP_X_FORWARDED_FOR' => isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '',
);
foreach ($_SERVER as $key => $value) {
    if (strpos($key, 'HTTP_') === 0 && is_string($value) && !isset($env[$key])) {
        $env[$key] = $value;
    }
}

list($ok, $raw, $err) = kasflow_proc(
    array(kasflow_venv_python($root), $root . '/plesk/cgi_app.py'),
    $root,
    $env,
    $body,
    120
);

if (!is_string($raw) || trim($raw) === '') {
    $log = kasflow_tail($err, 2);
    kasflow_fail($log !== '' ? 'De app stopte zonder antwoord. ' . $log : 'De app gaf geen antwoord.');
}

$sep = "\r\n\r\n";
$headerSize = strpos($raw, $sep);
if ($headerSize === false) {
    $sep = "\n\n";
    $headerSize = strpos($raw, $sep);
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
    if (preg_match('/^HTTP\/\d\.\d\s+(\d+)/i', $line, $match)) {
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
