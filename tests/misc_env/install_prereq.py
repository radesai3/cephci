import base64
import datetime
import itertools
import json
import time
import traceback

from ceph.parallel import parallel
from ceph.utils import config_ntp, update_ca_cert
from utility.log import Log
from utility.utils import get_cephci_config

log = Log(__name__)


rpm_packages = {
    "7": [
        "wget",
        "git-core",
        "python-virtualenv",
        "python-nose",
        "ntp",
        "python2-pip",
        "chrony",
    ],
    "all": [
        "wget",
        "git-core",
        "python3-devel",
        "chrony",
        "yum-utils",
        "net-tools",
        "lvm2",
        "podman",
        "net-snmp-utils",
        "net-snmp",
        "kernel-modules-extra",
        "iproute-tc",
    ],
}
deb_packages = ["wget", "git-core", "python-virtualenv", "lsb-release", "ntp"]
deb_all_packages = " ".join(deb_packages)


def run(**kw):
    log.info("Running test")
    ceph_nodes = kw.get("ceph_nodes")

    # skip subscription manager if testing beta RHEL
    config = kw.get("config")
    skip_subscription = config.get("skip_subscription", False)
    enable_eus = config.get("enable_eus", False)
    repo = config.get("add-repo", False)
    rhbuild = config.get("rhbuild")
    skip_enabling_rhel_rpms = config.get("skip_enabling_rhel_rpms", False)
    is_production = config.get("is_production", False)
    build_type = config.get("build_type", None)

    # when build set to released subscribing nodes with CDN credentials
    if build_type == "released":
        is_production = True

    cloud_type = config.get("cloud-type", "openstack")
    with parallel() as p:
        for ceph in ceph_nodes:
            p.spawn(
                install_prereq,
                ceph,
                1800,
                skip_subscription,
                repo,
                rhbuild,
                enable_eus,
                skip_enabling_rhel_rpms,
                is_production,
                cloud_type,
            )
            time.sleep(20)

    return 0


def install_prereq(
    ceph,
    timeout=1800,
    skip_subscription=False,
    repo=False,
    rhbuild=None,
    enable_eus=False,
    skip_enabling_rhel_rpms=False,
    is_production=False,
    cloud_type="openstack",
):
    log.info("Waiting for cloud config to complete on " + ceph.hostname)
    ceph.exec_command(cmd="while [ ! -f /ceph-qa-ready ]; do sleep 15; done")
    log.info("cloud config to completed on " + ceph.hostname)

    # Update certs
    update_ca_cert(
        node=ceph,
        cert_url="https://password.corp.redhat.com/RH-IT-Root-CA.crt",
        out_file="RH-IT-Root-CA.crt",
        check_ec=False,
    )

    # Update CephCI Cert to all nodes. Useful when creating self-signed certificates.
    update_ca_cert(
        node=ceph,
        cert_url="http://magna002.ceph.redhat.com/cephci-jenkins/.cephqe-ca.pem",
        out_file="cephqe-ca.pem",
        check_ec=False,
    )
    distro_info = ceph.distro_info
    distro_ver = distro_info["VERSION_ID"]
    log.info("distro name: {name}".format(name=distro_info["NAME"]))
    log.info("distro id: {id}".format(id=distro_info["ID"]))
    log.info(
        "distro version_id: {version_id}".format(version_id=distro_info["VERSION_ID"])
    )
    if ceph.pkg_type == "deb":
        ceph.exec_command(
            cmd="sudo apt-get install -y " + deb_all_packages, long_running=True
        )
    else:
        if distro_ver.startswith("7"):
            ceph.exec_command(cmd="sudo systemctl restart NetworkManager.service")

        if not skip_subscription:
            setup_subscription_manager(ceph, is_production, cloud_type)

            if not skip_enabling_rhel_rpms:
                if enable_eus:
                    enable_rhel_eus_rpms(ceph, distro_ver, cloud_type)
                else:
                    enable_rhel_rpms(ceph, distro_ver)
            else:
                log.info("Skipped enabling the RHEL RPM's provided by Subscription")

        if repo:
            setup_addition_repo(ceph, repo)

        ceph.exec_command(cmd="sudo yum -y upgrade", check_ec=False)

        rpm_all_packages = " ".join(rpm_packages.get("all"))
        if distro_ver.startswith("7"):
            rpm_all_packages = " ".join(rpm_packages.get("7"))

        ceph.exec_command(
            cmd=f"sudo yum install -y {rpm_all_packages}", long_running=True
        )

        # Restarting the node for qdisc filter to be loaded. This is required for
        # RHEL-8
        if not distro_ver.startswith("7"):
            # Avoiding early channel close and ignoring channel exception thrown during
            # reboot
            time.sleep(10)
            ceph.exec_command(sudo=True, cmd="reboot", check_ec=False)

            # Sleep before and after reconnect
            time.sleep(60)
            ceph.reconnect()
            time.sleep(10)

        if skip_enabling_rhel_rpms and skip_subscription:
            # Ansible is required for RHCS 4.x
            epel_pkg = "epel-release-latest-8.noarch.rpm"
            ansible_pkg = "ansible-2.9.27-1.el8"
            if distro_ver.startswith("7"):
                epel_pkg = "epel-release-latest-7.noarch.rpm"
                ansible_pkg = "ansible-2.9.27-1.el7"

            ceph.exec_command(
                sudo=True,
                cmd=f"yum install -y https://dl.fedoraproject.org/pub/epel/{epel_pkg}",
                check_ec=False,
            )
            ceph.exec_command(sudo=True, cmd=f"yum install -y {ansible_pkg}")

        if ceph.role == "client":
            ceph.exec_command(cmd="sudo yum install -y attr gcc", long_running=True)
            ceph.exec_command(cmd="sudo pip install crefi", long_running=True)

        ceph.exec_command(cmd="sudo yum clean all")
        config_ntp(ceph, cloud_type)

    registry_login(ceph, distro_ver)


def setup_addition_repo(ceph, repo):
    log.info("Adding addition repo {repo} to {sn}".format(repo=repo, sn=ceph.shortname))
    ceph.exec_command(
        sudo=True,
        cmd="curl -o /etc/yum.repos.d/rh_add_repo.repo {repo}".format(repo=repo),
    )
    ceph.exec_command(sudo=True, cmd="yum update metadata", check_ec=False)


def setup_subscription_manager(
    ceph, is_production=False, cloud_type="openstack", timeout=1800
):
    timeout = datetime.timedelta(seconds=timeout)
    starttime = datetime.datetime.now()
    log.info(
        "Subscribing {ip} host with {timeout} timeout".format(
            ip=ceph.ip_address, timeout=timeout
        )
    )
    while True:
        try:
            # subscription-manager tips:
            #
            # "--serverurl" (optional) is the entitlement service. The default
            # server (production) has customer-facing entitlement and SKU
            # information. The "stage" server has QE-only entitlement data.
            # We use Red Hat's internal "Ethel" tool to add SKUs to the
            # "rhcsuser" account that only exists in stage.
            #
            # "--baseurl" (optional) is the RPM content host. The default
            # value is the production CDN (cdn.redhat.com), and this hosts the
            # RPM contents to which all customers have access. Alternatively
            # you can push content to the staging CDN through the Errata Tool,
            # and then test it with --baseurl=cdn.stage.redhat.com.
            config_ = get_cephci_config()
            command = "sudo subscription-manager register --force "
            if is_production or cloud_type.startswith("ibmc"):
                command += "--serverurl=subscription.rhsm.redhat.com:443/subscription "
                username_ = config_["cdn_credentials"]["username"]
                password_ = config_["cdn_credentials"]["password"]
                pool_id = "8a85f99a7db4827d017dc5134ff800ba"

            else:
                command += (
                    "--serverurl=subscription.rhsm.stage.redhat.com:443/subscription "
                )
                username_ = config_["stage_credentials"]["username"]
                password_ = config_["stage_credentials"]["password"]
                pool_id = "8a99f9af795d57ab01797e572e860569"

            command += f"--baseurl=https://cdn.redhat.com --username={username_}"
            command += f" --password={password_}"

            ceph.exec_command(cmd=command, timeout=720, long_running=True)

            ceph.exec_command(
                cmd=f"sudo subscription-manager attach --pool {pool_id}",
                timeout=720,
                long_running=True,
            )
            break
        except (KeyError, AttributeError):
            required_key = "stage_credentials"
            if is_production or cloud_type.startswith("ibmc"):
                required_key = "cdn_credentials"

            raise RuntimeError(
                f"Require the {required_key} to be set in ~/.cephci.yaml, "
                "Please refer cephci.yaml.template"
            )
        except BaseException:  # noqa
            if datetime.datetime.now() - starttime > timeout:
                try:
                    out, err = ceph.exec_command(
                        cmd="cat /var/log/rhsm/rhsm.log", timeout=120
                    )
                    rhsm_log = out.read().decode()
                except BaseException:  # noqa
                    rhsm_log = "No Log Available"
                raise RuntimeError(
                    "Failed to subscribe {ip} with {timeout} timeout:"
                    "\n {stack_trace}\n\n rhsm.log:\n{log}".format(
                        ip=ceph.ip_address,
                        timeout=timeout,
                        stack_trace=traceback.format_exc(),
                        log=rhsm_log,
                    )
                )
            else:
                wait = iter(x for x in itertools.count(1, 10))
                time.sleep(next(wait))
    ceph.exec_command(
        cmd="sudo subscription-manager repos --disable=*", long_running=True
    )


def enable_rhel_rpms(ceph, distro_ver):
    """
    Setup cdn repositories for rhel systems
    Args:
        ceph:       cluster instance
        distro_ver: distro version details
    """

    repos = {
        "7": ["rhel-7-server-rpms", "rhel-7-server-extras-rpms"],
        "8": ["rhel-8-for-x86_64-appstream-rpms", "rhel-8-for-x86_64-baseos-rpms"],
        "9": ["rhel-9-for-x86_64-appstream-rpms", "rhel-9-for-x86_64-baseos-rpms"],
    }

    for repo in repos.get(distro_ver[0]):
        ceph.exec_command(
            sudo=True,
            cmd="subscription-manager repos --enable={r}".format(r=repo),
            long_running=True,
        )


def enable_rhel_eus_rpms(ceph, distro_ver, cloud_type="openstack"):
    """
    Setup cdn repositories for rhel systems
    reference: http://wiki.test.redhat.com/CEPH/SubscriptionManager
    Args:
        distro_ver:     distro version - example: 7.7
        ceph:           ceph object
        cloud_type:     System deployment environment
    """

    eus_repos = {"7": ["rhel-7-server-eus-rpms", "rhel-7-server-extras-rpms"]}

    if not cloud_type.startswith("ibmc"):
        # This pool ID would not work for production.
        ceph.exec_command(
            sudo=True,
            cmd="subscription-manager attach --pool 8a99f9ad77a7d7290177ce3852fc0c44",
            timeout=720,
        )
        ceph.exec_command(
            sudo=True, cmd="subscription-manager repos --disable=*", long_running=True
        )

    for repo in eus_repos.get(distro_ver[0]):
        ceph.exec_command(
            sudo=True,
            cmd="subscription-manager repos --enable={r}".format(r=repo),
            long_running=True,
        )

    rhel_major_version = distro_ver[0]

    if rhel_major_version == "7":
        # We only support one EUS release for RHEL 7:
        release = "7.7"
    else:
        raise NotImplementedError("cannot set EUS repos for %s", rhel_major_version)

    cmd = f"subscription-manager release --set={release}"

    ceph.exec_command(
        sudo=True,
        cmd=cmd,
        long_running=True,
    )

    ceph.exec_command(sudo=True, cmd="yum clean all", long_running=True)


def registry_login(ceph, distro_ver):
    """
    Login to the given Container registries provided in the configuration.

    In this method, docker or podman is installed based on OS.
    """
    container = "podman"
    if distro_ver.startswith("7"):
        container = "docker"

    ceph.exec_command(
        cmd="sudo yum install -y {c}".format(c=container), long_running=True
    )

    if container == "docker":
        ceph.exec_command(cmd="sudo systemctl restart docker", long_running=True)

    config = get_cephci_config()
    registries = [
        {
            "registry": "registry.redhat.io",
            "user": config["cdn_credentials"]["username"],
            "passwd": config["cdn_credentials"]["password"],
        }
    ]

    if (
        config.get("registry_credentials")
        and config["registry_credentials"]["registry"] != "registry.redhat.io"
    ):
        registries.append(
            {
                "registry": config["registry_credentials"]["registry"],
                "user": config["registry_credentials"]["username"],
                "passwd": config["registry_credentials"]["password"],
            }
        )
    auths = {}
    for r in registries:
        b64_auth = base64.b64encode(f"{r['user']}:{r['passwd']}".encode("ascii"))
        auths[r["registry"]] = {"auth": b64_auth.decode("utf-8")}
    auths_dict = {"auths": auths}
    ceph.exec_command(sudo=True, cmd="mkdir -p ~/.docker")
    ceph.exec_command(cmd="mkdir -p ~/.docker")
    auths_file_sudo = ceph.remote_file(
        sudo=True, file_name="/root/.docker/config.json", file_mode="w"
    )
    auths_file = ceph.remote_file(
        file_name="/home/cephuser/.docker/config.json", file_mode="w"
    )
    files = [auths_file_sudo, auths_file]
    for file in files:
        file.write(json.dumps(auths_dict, indent=4))
        file.flush()
        file.close()
