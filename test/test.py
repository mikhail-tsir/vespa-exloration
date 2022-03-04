#! /usr/bin/env python3
# Copyright Yahoo. Licensed under the terms of the Apache 2.0 license. See LICENSE in the project root.

import io
import os
import sys
import getopt
import json
import time
import yaml
import urllib.request
import tempfile
import re

from bs4 import BeautifulSoup

from pseudo_terminal import PseudoTerminal

################################################################################
# Execution
################################################################################

verbose = False
project_root = os.getcwd()
work_dir = os.path.join(project_root, "_work")


def print_cmd_header(cmd, extra="", print_header=True):
    if not print_header:
        return
    print("")
    print("*" * 80)
    print("* {0}".format(cmd))
    if len(extra) > 0:
        print("* ({0})".format(extra))
    print("*" * 80)


def exec_wait(cmd, pty):
    command = cmd["$"]
    expect = cmd["wait-for"]
    max_wait = 300  # todo: max this configurable
    try_interval = 5  # todo: max this configurable too
    print_cmd_header(command, "Waiting for '{0}'".format(expect))

    waited = 0
    output = ""
    while waited < max_wait:
        exit_code, output = pty.run(command, verbose)
        if output.find(expect) >= 0:
            return
        else:
            time.sleep(try_interval)
            waited += try_interval
            print("Waited for {0}/{1} seconds...".format(waited, max_wait))

    if waited >= max_wait:
        if not verbose:
            print(output)
        raise RuntimeError("Expected output '{0}' not found in command '{1}'. Waited for {2} seconds.".format(expect, command, max_wait))


def exec_assert(cmd, pty):
    command = cmd["$"]
    expect = cmd["contains"]
    print_cmd_header(command, "Expecting '{0}'".format(expect))

    _, output = pty.run(command, verbose)
    if output.find(expect) == -1:
        if not verbose:
            print(output)
        raise RuntimeError("Expected output '{0}' not found in command '{1}'".format(expect, command))


def exec_file(cmd, pty):
    path = cmd["path"]
    print_cmd_header(path)
    path_array = []
    for dir in path.split(os.path.sep)[:-1]:
        path_array.append(dir)
        if not os.path.isdir(os.path.sep.join(path_array)):
            os.makedirs(os.path.sep.join(path_array))
    with open(str(path), "w") as f:
        f.write(str(cmd["content"]))

    print("Wrote " + str(len(cmd["content"])) + " chars to " + path)


def exec_expect(cmd, pty):
    command = cmd["$"]
    expect = cmd["expect"]
    timeout = cmd["timeout"]
    print_cmd_header(command, "Expecting '{0}'".format(expect))

    exit_code, output = pty.run_expect(command, expect, timeout, verbose)
    if exit_code != 0:
        if not verbose:
            print(output)
        raise RuntimeError("Command '{0}' returned code {1}".format(command, exit_code))


def exec_default(cmd, pty):
    command = cmd["$"]
    print_cmd_header(command)

    exit_code, output = pty.run(command, verbose)
    if exit_code != 0:
        if not verbose:
            print(output)
        raise RuntimeError("Command '{0}' returned code {1}".format(command, exit_code))


def exec_step(cmd, pty):
    globals()["exec_" + cmd["type"]](cmd, pty)


def exec_script(script):
    tmpdir = tempfile.mkdtemp(dir=work_dir)
    os.chdir(tmpdir)

    failed = False

    with PseudoTerminal(timeout=2*60*60) as pty:
        try:
            for cmd in script["before"]:
                exec_step(cmd, pty)
            for cmd in script["steps"]:
                exec_step(cmd, pty)
        except Exception as e:
            sys.stderr.write("ERROR: {0}\n".format(e))
            failed = True
        finally:
            for cmd in script["after"]:
                try:
                    exec_step(cmd, pty)
                except Exception as e:
                    sys.stderr.write("ERROR: {0}\n".format(e))
                    failed = True

    if failed:
        raise RuntimeError("One or more commands failed")


################################################################################
# Parsing
################################################################################

def parse_cmd(cmd, attrs):
    cmd = cmd.strip()
    if cmd.startswith("#"):
        return None
    if cmd.startswith("$"):
        cmd = cmd[1:]
    cmd = cmd.strip()
    if len(cmd) == 0:
        return None

    if "data-test-wait-for" in attrs:
        return {"$": cmd, "type": "wait", "wait-for": attrs["data-test-wait-for"]}
    if "data-test-assert-contains" in attrs:
        return {"$": cmd, "type": "assert", "contains": attrs["data-test-assert-contains"]}
    if "data-test-expect" in attrs:
        return {"$": cmd, "type": "expect", "expect": attrs["data-test-expect"], "timeout": attrs["data-test-timeout"]}
    return {"$": cmd, "type": "default"}


def parse_cmds(pre, attrs):
    cmds = []
    line_continuation = ""
    line_continuation_delimiter = "\\"

    # Remove liquid macros, if present, e.g:
    # {% highlight shell %}       {% endhighlight %}
    # {% raw %}                   {% endraw %}
    sanitized     = re.sub(r"{%\s*.*highlight\s*.*%}", "", pre)
    sanitized_pre = re.sub(r"{%\s*.*raw\s*%}", "", sanitized)

    for line in sanitized_pre.split("\n"):
        cmd = "{0} {1}".format(line_continuation, line.strip())
        if cmd.endswith(line_continuation_delimiter):
            line_continuation = cmd[:-len(line_continuation_delimiter)]
        else:
            cmd = parse_cmd(cmd, attrs)
            if cmd != None:
                cmds.append(cmd)
            line_continuation = ""
    return cmds


def parse_file(pre, attrs):
    if not "data-path" in attrs:
        raise ValueError("File element does not have required 'data-path' attribute.")
    path = attrs["data-path"]
    if path[0] == "/":
        raise ValueError("Absolute file paths are not permitted")
    if ".." in path:
        raise ValueError("'..' not permitted in file paths")
    content = ""
    for line in pre:
        if "ProcessingInstruction" in str(type(line)):  # xml: <? ... ?>
            content += "<?" + str(line) + ">"
        else:
            content += str(line)
    content = content[1:] if content[0] == "\n" else content
    return {"type": "file", "content": content, "path": path}


def parse_page(html):
    script = {
        "before": [],
        "steps": [],
        "after": []
    }

    soup = BeautifulSoup(html, "html.parser")

    for pre in soup.find_all(lambda tag: (tag.name == "pre" or tag.name == "div") and tag.has_attr("data-test")):
        if pre.attrs["data-test"] == "before":
            script["before"].extend(parse_cmds(pre.string, pre.attrs))

        if pre.attrs["data-test"] == "exec":
            script["steps"].extend(parse_cmds(pre.string, pre.attrs))

        if pre.attrs["data-test"] == "file":
            script["steps"].append(parse_file(pre.contents, pre.attrs))

        if pre.attrs["data-test"] == "after":
            script["after"].extend(parse_cmds(pre.string, pre.attrs))

    return script


def process_page(html, source_name=""):
    script = parse_page(html)

    print_cmd_header("Script to execute", extra=source_name)
    print(json.dumps(script, indent=2))

    exec_script(script)


################################################################################
# Running
################################################################################

def create_work_dir():
    os.chdir(project_root)
    if not os.path.isdir(work_dir):
        os.makedirs(work_dir)


def run_url(url):
    print_cmd_header("Testing", url)
    allpages = b""
    for page in url.split(","):
        allpages += urllib.request.urlopen(page).read()

    process_page(allpages, url)


def run_config(config_file):
    failed = []
    if not os.path.isfile(config_file):
        config_file = os.path.join("test", config_file)
    if not os.path.isfile(config_file):
        raise RuntimeError("Could not find configuration file")

    with open(config_file, "r") as f:
        config = yaml.safe_load(f)
        for url in config["urls"]:
            try:
                run_url(url)
            except RuntimeError:
                failed.append(url)

    if len(failed) > 0:
        raise RuntimeError("One or more files failed: " + ", ".join(failed))


def run_file(file_name):
    if file_name.startswith("http"):
        run_url(file_name)
    elif file_name == "-":
        process_page(sys.stdin.read(), "stdin")
    else:
        with io.open(file_name, 'r', encoding="utf-8") as f:
            process_page(f.read(), file_name)


def run_with_arguments():
    global verbose
    config_file = ""
    argv = sys.argv[1:]

    try:
        opts, args = getopt.getopt(argv, "vc:")
    except getopt.GetoptError:
        print("test.py [-v] [-c configfile] [file-to-run]")
        sys.exit(2)

    for opt, arg in opts:
        if opt in "-v":
            verbose = True
        elif opt in "-c":
            config_file = arg

    if len(config_file):
        run_config(config_file)
    elif args:
        run_file(args[0])
    else:
        run_config("_test_config.yml")


def main():
    create_work_dir()

    try:
        run_with_arguments()
    except Exception as e:
        sys.stderr.write("ERROR: {0}\n".format(e))
        sys.exit(1)

if __name__ == "__main__":
    main()
