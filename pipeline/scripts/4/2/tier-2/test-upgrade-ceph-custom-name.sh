#! /bin/sh
echo "Beginning Red Hat Ceph RPM based upgrade with ceph custom name - OKR bz 1876447 testing."

random_string=$(cat /dev/urandom | tr -cd 'a-z0-9' | head -c 5)
instance_name="ci-${random_string}"
platform="rhel-7"
rhbuild="4.2"
test_suite="suites/nautilus/upgrades/tier-2_upgrade_ceph-custom-name.yaml"
test_conf="conf/nautilus/upgrades/upgrades.yaml"
test_inventory="conf/inventory/rhel-7-latest.yaml"
return_code=0

# Process the CLI arguments for IBM-C environment
CLI_ARGS=$@
cloud="ibmc"
if [ -z "${CLI_ARGS##*$cloud*}" ] ; then
    test_inventory="conf/inventory/ibm-vpc-rhel-7-latest.yaml"
else
    CLI_ARGS="$CLI_ARGS --post-results --report-portal"
fi

$WORKSPACE/.venv/bin/python run.py \
    --osp-cred $HOME/osp-cred-ci-2.yaml \
    --rhbuild $rhbuild \
    --platform $platform \
    --instances-name $instance_name \
    --global-conf $test_conf \
    --suite $test_suite \
    --inventory $test_inventory \
    --log-level DEBUG \
    $CLI_ARGS

if [ $? -ne 0 ]; then
    return_code=1
fi

CLEANUP_ARGS="--log-level debug --osp-cred $HOME/osp-cred-ci-2.yaml"
if [ -z "${CLI_ARGS##*$cloud*}" ] ; then
    CLEANUP_ARGS="$CLEANUP_ARGS --cloud ibmc"
fi

$WORKSPACE/.venv/bin/python run.py --cleanup $instance_name $CLEANUP_ARGS

if [ $? -ne 0 ]; then
    echo "cleanup instance failed for instance $instance_name"
fi

exit ${return_code}

