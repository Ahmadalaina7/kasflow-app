<?php
/**
 * TIJDELIJK diagnosepagina. Geen geheimen: alleen versies, paden en rechten.
 * Verwijderen zodra de oorzaak bekend is.
 */
header('Content-Type: text/plain; charset=utf-8');

function run($cmd, $cwd = null)
{
    $disabled = array_map('trim', explode(',', (string) ini_get('disable_functions')));
    if (!function_exists('proc_open') || in_array('proc_open', $disabled, true)) {
        return 'PROC_OPEN_UIT';
    }
    $spec = array(0 => array('pipe', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w'));
    $proc = @proc_open($cmd, $spec, $pipes, $cwd);
    if (!is_resource($proc)) {
        return 'GEEN_PROCES';
    }
    fclose($pipes[0]);
    $out = stream_get_contents($pipes[1]);
    $err = stream_get_contents($pipes[2]);
    fclose($pipes[1]);
    fclose($pipes[2]);
    $code = proc_close($proc);
    return 'exit=' . $code . ' out=' . trim((string) $out) . ' err=' . trim((string) $err);
}

$root = __DIR__;
echo "PHP " . PHP_VERSION . " sapi=" . PHP_SAPI . "\n";
echo "disable_functions=" . ini_get('disable_functions') . "\n";
echo "open_basedir=" . ini_get('open_basedir') . "\n";
echo "root=$root\n";
echo "user=" . (function_exists('posix_getpwuid') && function_exists('posix_geteuid') ? posix_getpwuid(posix_geteuid())['name'] : '?') . "\n";
echo "uname=" . php_uname() . "\n\n";

echo "== python kandidaten ==\n";
$py = null;
foreach (array(
    '/opt/plesk/python/3.13/bin/python3', '/opt/plesk/python/3.12/bin/python3',
    '/opt/plesk/python/3.11/bin/python3', '/opt/plesk/python/3.10/bin/python3',
    '/opt/plesk/python/3.9/bin/python3', '/usr/bin/python3.12', '/usr/bin/python3.11',
    '/usr/bin/python3.10', '/usr/bin/python3.9', '/usr/bin/python3', '/usr/bin/python',
) as $c) {
    if (is_file($c) || is_link($c)) {
        $v = run(array($c, '--version'));
        echo "$c exists: $v\n";
        if ($py === null && strpos($v, 'Python 3') === 0) {
            $py = $c;
        }
    } else {
        echo "$c ontbreekt\n";
    }
}
echo "which python3: " . run(array('/usr/bin/env', 'python3', '--version')) . "\n";
echo "which python:  " . run(array('/usr/bin/env', 'python', '--version')) . "\n\n";

echo "== venv ==\n";
$venv = $root . '/venv';
echo "venv dir: " . (is_dir($venv) ? 'ja' : 'NEE') . "\n";
echo "venv python: " . (is_file($venv . '/bin/python') ? 'ja' : 'NEE') . " executable=" . (is_executable($venv . '/bin/python') ? 'ja' : 'nee') . "\n";
if (is_file($venv . '/bin/python')) {
    echo "venv --version: " . run(array($venv . '/bin/python', '--version')) . "\n";
    echo "venv site-packages: " . run(array($venv . '/bin/python', '-c', 'import sys;print(sys.prefix)')) . "\n";
    echo "imports: " . run(array($venv . '/bin/python', '-c',
        'mods=["fastapi","uvicorn","starlette","pydantic","sqlalchemy","dotenv","a2wsgi","reportlab","psycopg","httpx","itsdangerous"];' .
        'import importlib;[print(m, ("ok" if importlib.util.find_spec(m) else "MIST")) for m in mods]')) . "\n";
}
echo "pip: " . (is_file($venv . '/bin/pip') ? 'ja' : 'NEE') . "\n\n";

echo "== .env ==\n";
if (is_file($root . '/.env')) {
    echo "aanwezig, " . filesize($root . '/.env') . " bytes\n";
    foreach (file($root . '/.env', FILE_IGNORE_NEW_LINES) as $line) {
        $line = trim($line);
        if ($line === '' || $line[0] === '#') {
            continue;
        }
        $parts = explode('=', $line, 2);
        $key = $parts[0];
        $value = isset($parts[1]) ? $parts[1] : '';
        if (preg_match('/KEY|PASSWORD|SECRET|TOKEN/i', $key)) {
            echo "$key=<verborgen, " . strlen($value) . " tekens>\n";
        } elseif (preg_match('/DATABASE_URL/', $key)) {
            echo "$key=" . preg_replace('/:[^:@\/]+@/', ':***@', $value) . "\n";
        } else {
            echo "$key=$value\n";
        }
    }
} else {
    echo ".env ONTBREEKT\n";
}
echo "\n";

echo "== schrijfrechten ==\n";
foreach (array('', '/backend', '/backend/logs', '/backend/uploads', '/venv') as $d) {
    $p = $root . $d;
    echo ($p === $root ? '.' : $d) . ": " . (is_dir($p) ? 'dir' : 'GEEN DIR') . " writable=" . (is_writable($p) ? 'ja' : 'nee') . "\n";
}
echo "vrije ruimte=" . @disk_free_space($root) . " totaal=" . @disk_total_space($root) . "\n\n";

echo "== logs ==\n";
foreach (array('/backend/logs/cgi.log', '/backend/logs/error.log', '/backend/logs/access.log') as $f) {
    echo "-- $f: " . (is_file($root . $f) ? filesize($root . $f) . " bytes" : 'ONTBREEKT') . "\n";
    if (is_file($root . $f)) {
        $lines = array_slice(file($root . $f), -25);
        echo implode('', $lines) . "\n";
    }
}
echo "\n== tabellen ==\n";
foreach (glob($root . '/backend/*.sqlite3') as $f) {
    echo "$f " . filesize($f) . " bytes\n";
}
echo "klaar\n";
