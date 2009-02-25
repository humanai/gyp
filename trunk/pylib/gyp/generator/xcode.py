#!/usr/bin/python

import filecmp
import gyp.common
import gyp.xcodeproj_file
import errno
import os
import pprint
import re
import shutil
import tempfile


# Project files generated by this module will use _intermediate_var as a
# custom Xcode setting whose value is a DerivedSources-like directory that's
# project-specific and configuration-specific.  The normal choice,
# DERIVED_FILE_DIR, is target-specific, which is thought to be too restrictive
# as it is likely that multiple targets within a single project file will want
# to access the same set of generated files.  The other option,
# PROJECT_DERIVED_FILE_DIR, is unsuitable because while it is project-specific,
# it is not configuration-specific.  INTERMEDIATE_DIR is defined as
# $(PROJECT_DERIVED_FILE_DIR)/$(CONFIGURATION).
_intermediate_var = 'INTERMEDIATE_DIR'

# SHARED_INTERMEDIATE_DIR is the same, except that it is shared among all
# targets that share the same BUILT_PRODUCTS_DIR.
_shared_intermediate_var = 'SHARED_INTERMEDIATE_DIR'

generator_default_variables = {
  'EXECUTABLE_PREFIX': '',
  'EXECUTABLE_SUFFIX': '',
  # INTERMEDIATE_DIR is a place for targets to build up intermediate products.
  # It is specific to each build environment.  It is only guaranteed to exist
  # and be constant within the context of a project, corresponding to a single
  # input file.  Some build environments may allow their intermediate directory
  # to be shared on a wider scale, but this is not guaranteed.
  'INTERMEDIATE_DIR': '$(%s)' % _intermediate_var,
  'OS': 'mac',
  'PRODUCT_DIR': '$(BUILT_PRODUCTS_DIR)',
  'RULE_INPUT_ROOT': '$(INPUT_FILE_BASE)',
  'RULE_INPUT_EXT': '$(INPUT_FILE_SUFFIX)',
  'RULE_INPUT_NAME': '$(INPUT_FILE_NAME)',
  'RULE_INPUT_PATH': '$(INPUT_FILE_PATH)',
  'SHARED_INTERMEDIATE_DIR': '$(%s)' % _shared_intermediate_var,
}


class XcodeProject(object):
  def __init__(self, gyp_path, path, build_file_dict):
    self.gyp_path = gyp_path
    self.path = path
    self.project = gyp.xcodeproj_file.PBXProject(path=path)
    self.project_file = \
        gyp.xcodeproj_file.XCProjectFile({'rootObject': self.project})
    self.build_file_dict = build_file_dict
    self.run_test_targets = []

    # TODO(mark): add destructor that cleans up self.path if created_dir is
    # True and things didn't complete successfully.  Or do something even
    # better with "try"?
    self.created_dir = False
    try:
      os.mkdir(self.path)
      self.created_dir = True
    except OSError, e:
      if e.errno != errno.EEXIST:
        raise

  def Finalize1(self, xcode_targets, build_file_dict):
    # Collect a list of all of the build configuration names used by the
    # various targets in the file.  It is very heavily advised to keep each
    # target in an entire project (even across multiple project files) using
    # the same set of configuration names.
    configurations = []
    for xct in self.project.GetProperty('targets'):
      xccl = xct.GetProperty('buildConfigurationList')
      xcbcs = xccl.GetProperty('buildConfigurations')
      for xcbc in xcbcs:
        name = xcbc.GetProperty('name')
        if name not in configurations:
          configurations.append(name)

    # Replace the XCConfigurationList attached to the PBXProject object with
    # a new one specifying all of the configuration names used by the various
    # targets.
    xccl = gyp.xcodeproj_file.XCConfigurationList({'buildConfigurations': []})
    for configuration in configurations:
      xcbc = gyp.xcodeproj_file.XCBuildConfiguration({'name': configuration})
      xccl.AppendProperty('buildConfigurations', xcbc)
    xccl.SetProperty('defaultConfigurationName', configurations[0])
    self.project.SetProperty('buildConfigurationList', xccl)

    # The need for this setting is explained above where _intermediate_var is
    # defined.  The comments below about wanting to avoid project-wide build
    # settings apply here too, but this needs to be set on a project-wide basis
    # so that files relative to the _intermediate_var setting can be displayed
    # properly in the Xcode UI.
    #
    # Note that for configuration-relative files such as anything relative to
    # _intermediate_var, for the purposes of UI tree view display, Xcode will
    # only resolve the configuration name once, when the project file is
    # opened.  If the active build configuration is changed, the project file
    # must be closed and reopened if it is desired for the tree view to update.
    # This is filed as Apple radar 6588391.
    xccl.SetBuildSetting(_intermediate_var,
                         '$(PROJECT_DERIVED_FILE_DIR)/$(CONFIGURATION)')
    xccl.SetBuildSetting(_shared_intermediate_var,
                         '$(SYMROOT)/DerivedSources/$(CONFIGURATION)')

    # Set user-specified project-wide build settings.  This is intended to be
    # used very sparingly.  Really, almost everything should go into
    # target-specific build settings sections.  The project-wide settings are
    # only intended to be used in cases where Xcode attempts to resolve
    # variable references in a project context as opposed to a target context,
    # such as when resolving sourceTree references while building up the tree
    # tree view for UI display.
    for xck, xcv in self.build_file_dict.get('xcode_settings', {}).iteritems():
      xccl.SetBuildSetting(xck, xcv)

    # Sort the targets based on how they appeared in the input.
    # TODO(mark): Like a lot of other things here, this assumes internal
    # knowledge of PBXProject - in this case, of its "targets" property.
    targets = []
    non_runtest_targets = []
    runtest_targets = []
    for target in build_file_dict['targets']:
      target_name = target['target_name']
      qualified_target = gyp.common.QualifiedTarget(self.gyp_path, target_name)
      xcode_target = xcode_targets[qualified_target]
      # Make sure that the target being added to the sorted list is already in
      # the unsorted list.
      assert xcode_target in self.project._properties['targets']
      targets.append(xcode_targets[qualified_target])
      non_runtest_targets.append(xcode_targets[qualified_target])

      # If this is a test target, add its test runner target.
      if target.get('test', 0):
        # Make a target to run the test.  It should have one dependency, the
        # test executable target.
        run_target = gyp.xcodeproj_file.PBXAggregateTarget({
              'name':        'Run "' + target_name + '"',
              'productName': xcode_target.GetProperty('productName'),
            },
            parent=self.project)
        run_target.AddDependency(xcode_target)

        # The test runner target has a build phase that executes the test.
        script = 'exec "${BUILT_PRODUCTS_DIR}/${PRODUCT_NAME}"\nexit 1\n'
        ssbp = gyp.xcodeproj_file.PBXShellScriptBuildPhase({
              'shellScript':      script,
              'showEnvVarsInLog': 0,
            })
        run_target.AppendProperty('buildPhases', ssbp)

        # Add the test runner target to the project file.
        xcode_target.test_runner = run_target
        targets.append(run_target)
        runtest_targets.append(run_target)

    # Make sure that the list of targets being replaced is the same length as
    # the one replacing it, but allow for the added test runner targets.
    assert len(self.project._properties['targets']) + len(non_runtest_targets)

    self.project._properties['targets'] = targets

    # Get rid of unnecessary levels of depth in groups like the Source group.
    self.project.RootGroupsTakeOverOnlyChildren(True)

    # Sort the groups nicely.  Do this after sorting the targets, because the
    # Products group is sorted based on the order of the targets.
    self.project.SortGroups()

    # Create an "All" target if there's more than one target in this project
    # file.  Put the "All" target it first so that people opening up the
    # project for the first time will build everything by default.
    if len(non_runtest_targets) > 1:
      xccl = gyp.xcodeproj_file.XCConfigurationList({'buildConfigurations': []})
      for configuration in configurations:
        xcbc = gyp.xcodeproj_file.XCBuildConfiguration({'name': configuration})
        xccl.AppendProperty('buildConfigurations', xcbc)
      xccl.SetProperty('defaultConfigurationName', configurations[0])

      all_target = gyp.xcodeproj_file.PBXAggregateTarget(
          {
            'buildConfigurationList': xccl,
            'name':                   'All',
          },
          parent=self.project)

      for target in non_runtest_targets:
        all_target.AddDependency(target)

      # TODO(mark): This is evil because it relies on internal knowledge of
      # PBXProject._properties.  It's important to get the "All" target first,
      # though.
      self.project._properties['targets'].insert(0, all_target)

    # The same, but for runtest targets.
    if len(runtest_targets) > 1:
      xccl = gyp.xcodeproj_file.XCConfigurationList({'buildConfigurations': []})
      for configuration in configurations:
        xcbc = gyp.xcodeproj_file.XCBuildConfiguration({'name': configuration})
        xccl.AppendProperty('buildConfigurations', xcbc)
      xccl.SetProperty('defaultConfigurationName', configurations[0])

      run_all_tests_target = gyp.xcodeproj_file.PBXAggregateTarget(
          {
            'buildConfigurationList': xccl,
            'name':                   'Run All Tests',
          },
          parent=self.project)

      for target in runtest_targets:
        run_all_tests_target.AddDependency(target)

      # Insert after the "All" target, which must exist if there is more than
      # one run_test_target.
      self.project._properties['targets'].insert(1, run_all_tests_target)

  def Finalize2(self, xcode_targets):
    # Finalize2 needs to happen in a separate step because the process of
    # updating references to other projects depends on the ordering of targets
    # within remote project files.  Finalize1 is responsible for sorting duty,
    # and once all project files are sorted, Finalize2 can come in and update
    # these references.

    # TODO(mark): Make more general?
    # As a special case for Chromium, add a "Run All Tests" target in
    # all.gyp:All to depend on all test runners.
    if os.path.basename(self.gyp_path) == 'all.gyp' and \
       len(self.build_file_dict['targets']) == 1 and \
       self.build_file_dict['targets'][0]['target_name'] == 'All':
      qualified_target = gyp.common.QualifiedTarget(self.gyp_path, 'All')
      xcode_target = xcode_targets[qualified_target]
      if isinstance(xcode_target, gyp.xcodeproj_file.PBXAggregateTarget):
        # Collect all the test runner targets, built in Finalize1.
        all_test_runners = []
        pbxtds = xcode_target.GetProperty('dependencies')
        for pbxtd in pbxtds:
          pbxcip = pbxtd.GetProperty('targetProxy')
          dependency_xct = pbxcip.GetProperty('remoteGlobalIDString')
          if dependency_xct.test_runner:
            all_test_runners.append(dependency_xct.test_runner)

        # Set up a target that depends on all of the other test runners.
        if len(all_test_runners) > 0:
          run_all_target = gyp.xcodeproj_file.PBXAggregateTarget({
                'name':        'Run All Tests',
                'productName': 'All',
              },
              parent=self.project)
          for test_runner in all_test_runners:
            run_all_target.AddDependency(test_runner)

          # Add the test runner target to the project file.
          self.project.AppendProperty('targets', run_all_target)

    # Update all references to other projects, to make sure that the lists of
    # remote products are complete.  Otherwise, Xcode will fill them in when
    # it opens the project file, which will result in unnecessary diffs.
    # TODO(mark): This is evil because it relies on internal knowledge of
    # PBXProject._other_pbxprojects.
    for other_pbxproject in self.project._other_pbxprojects.keys():
      self.project.AddOrGetProjectReference(other_pbxproject)

    self.project.SortRemoteProductReferences()

    # Give everything an ID.
    self.project_file.ComputeIDs()

    # Make sure that no two objects in the project file have the same ID.  If
    # multiple objects wind up with the same ID, upon loading the file, Xcode
    # will only recognize one object (the last one in the file?) and the
    # results are unpredictable.
    self.project_file.EnsureNoIDCollisions()

  def Write(self):
    # Write the project file to a temporary location first.  Xcode watches for
    # changes to the project file and presents a UI sheet offering to reload
    # the project when it does change.  However, in some cases, especially when
    # multiple projects are open or when Xcode is busy, things don't work so
    # seamlessly.  Sometimes, Xcode is able to detect that a project file has
    # changed but can't unload it because something else is referencing it.
    # To mitigate this problem, and to avoid even having Xcode present the UI
    # sheet when an open project is rewritten for inconsequential changes, the
    # project file is written to a temporary file in the xcodeproj directory
    # first.  The new temporary file is then compared to the existing project
    # file, if any.  If they differ, the new file replaces the old; otherwise,
    # the new project file is simply deleted.  Xcode properly detects a file
    # being renamed over an open project file as a change and so it remains
    # able to present the "project file changed" sheet under this system.
    # Writing to a temporary file first also avoids the possible problem of
    # Xcode rereading an incomplete project file.
    (output_fd, new_pbxproj_path) = \
        tempfile.mkstemp(suffix='.tmp', prefix='project.pbxproj.gyp.',
                         dir=self.path)

    try:
      output_file = os.fdopen(output_fd, 'w')

      self.project_file.Print(output_file)
      output_file.close()

      pbxproj_path = os.path.join(self.path, 'project.pbxproj')

      same = False
      try:
        same = filecmp.cmp(pbxproj_path, new_pbxproj_path, False)
      except OSError, e:
        if e.errno != errno.ENOENT:
          raise

      if same:
        # The new file is identical to the old one, just get rid of the new
        # one.
        os.unlink(new_pbxproj_path)
      else:
        # The new file is different from the old one, or there is no old one.
        # Rename the new file to the permanent name.
        #
        # tempfile.mkstemp uses an overly restrictive mode, resulting in a
        # file that can only be read by the owner, regardless of the umask.
        # There's no reason to not respect the umask here, which means that
        # an extra hoop is required to fetch it and reset the new file's mode.
        #
        # No way to get the umask without setting a new one?  Set a safe one
        # and then set it back to the old value.
        umask = os.umask(077)
        os.umask(umask)

        os.chmod(new_pbxproj_path, 0666 & ~umask)

        os.rename(new_pbxproj_path, pbxproj_path)

    except Exception:
      # Don't leave turds behind.  In fact, if this code was responsible for
      # creating the xcodeproj directory, get rid of that too.
      os.unlink(new_pbxproj_path)
      if self.created_dir:
        shutil.rmtree(self.path, True)
      raise


def AddSourceToTarget(source, pbxp, xct):
  # TODO(mark): Perhaps this can be made a little bit fancier.
  source_extensions = ['c', 'cc', 'cpp', 'm', 'mm', 's']
  basename = os.path.basename(source)
  (root, ext) = os.path.splitext(basename)
  if ext != '':
    ext = ext[1:]

  if ext in source_extensions:
    xct.SourcesPhase().AddFile(source)
  else:
    # Files that aren't added to a sources build phase can still go into
    # the project file, just not as part of a build phase.
    pbxp.AddOrGetFileInRootGroup(source)


def AddResourceToTarget(resource, pbxp, xct):
  # TODO(mark): Combine with AddSourceToTarget above?  Or just inline this call
  # where it's used.
  xct.ResourcesPhase().AddFile(resource)


_xcode_variable_re = re.compile('(\$\((.*?)\))')
def ExpandXcodeVariables(string, expansions):
  """Expands Xcode-style $(VARIABLES) in string per the expansions dict.

  In some rare cases, it is appropriate to expand Xcode variables when a
  project file is generated.  For any substring $(VAR) in string, if VAR is a
  key in the expansions dict, $(VAR) will be replaced with expansions[VAR].
  Any $(VAR) substring in string for which VAR is not a key in the expansions
  dict will remain in the returned string.
  """

  matches = _xcode_variable_re.findall(string)
  if matches == None:
    return string

  matches.reverse()
  for match in matches:
    (to_replace, variable) = match
    if not variable in expansions:
      continue

    replacement = expansions[variable]
    string = re.sub(re.escape(to_replace), replacement, string)

  return string


def GenerateOutput(target_list, target_dicts, data):
  xcode_projects = {}
  for build_file, build_file_dict in data.iteritems():
    if build_file[-4:] != '.gyp':
      continue
    build_file_stem = build_file[:-4]
    # TODO(mark): To keep gyp-generated xcodeproj bundles from colliding with
    # checked-in versions, temporarily put _gyp into the ones created here.
    xcp = XcodeProject(build_file, build_file_stem + '_gyp.xcodeproj',
                       build_file_dict)
    xcode_projects[build_file] = xcp
    pbxp = xcp.project

    main_group = pbxp.GetProperty('mainGroup')
    build_group = gyp.xcodeproj_file.PBXGroup({'name': 'Build'})
    main_group.AppendChild(build_group)
    for included_file in build_file_dict['included_files']:
      build_group.AddOrGetFileByPath(included_file, False)

  xcode_targets = {}
  for qualified_target in target_list:
    [build_file, target_name] = \
        gyp.common.BuildFileAndTarget('', qualified_target)[0:2]
    spec = target_dicts[qualified_target]
    configuration_names = [spec['default_configuration']]
    for configuration_name in sorted(spec['configurations'].keys()):
      if configuration_name not in configuration_names:
        configuration_names.append(configuration_name)
    xcp = xcode_projects[build_file]
    pbxp = xcp.project

    # Set up the configurations for the target according to the list of names
    # supplied.
    xccl = gyp.xcodeproj_file.XCConfigurationList({'buildConfigurations': []})
    for configuration_name in configuration_names:
      xcbc = gyp.xcodeproj_file.XCBuildConfiguration({
          'name': configuration_name})
      xccl.AppendProperty('buildConfigurations', xcbc)
    xccl.SetProperty('defaultConfigurationName', configuration_names[0])

    # Create an XCTarget subclass object for the target.
    _types = {
      'application':    'com.apple.product-type.application',
      'executable':     'com.apple.product-type.tool',
      'shared_library': 'com.apple.product-type.library.dynamic',
      'static_library': 'com.apple.product-type.library.static',
    }

    target_properties = {
      'buildConfigurationList': xccl,
      'name':                   target_name,
    }

    type = spec['type']
    if type != 'none':
      xctarget_type = gyp.xcodeproj_file.PBXNativeTarget
      target_properties['productType'] = _types[type]
    else:
      xctarget_type = gyp.xcodeproj_file.PBXAggregateTarget

    if 'product_name' in spec:
      target_properties['productName'] = spec['product_name']

    xct = xctarget_type(target_properties, parent=pbxp)
    pbxp.AppendProperty('targets', xct)
    xcode_targets[qualified_target] = xct

    prebuild_index = 0

    # Add custom shell script phases for "actions" sections.
    for action in spec.get('actions', []):
      # There's no need to write anything into the script to ensure that the
      # output directories already exist, because Xcode will look at the
      # declared outputs and automatically ensure that they exist for us.

      # Turn the list into a string that can be passed to a shell.
      action_string = gyp.common.EncodePOSIXShellList(action['action'])

      # Convert Xcode-type variable references to sh-compatible environment
      # variable references.
      action_string_sh = re.sub('\$\((.*?)\)', '${\\1}', action_string)

      # Be sure the script runs in exec, and that if exec fails, the script
      # exits signalling an error.
      script = 'exec ' + action_string_sh + '\nexit 1\n'
      ssbp = gyp.xcodeproj_file.PBXShellScriptBuildPhase({
            'inputPaths': action['inputs'],
            'name': 'Action "' + action['action_name'] + '"',
            'outputPaths': action['outputs'],
            'shellScript': script,
            'showEnvVarsInLog': 0,
          })

      # TODO(mark): this assumes too much knowledge of the internals of
      # xcodeproj_file; some of these smarts should move into xcodeproj_file
      # itself.
      xct._properties['buildPhases'].insert(prebuild_index, ssbp)
      prebuild_index = prebuild_index + 1

      if action.get('process_outputs_as_sources', False):
        for output in action['outputs']:
          AddSourceToTarget(output, pbxp, xct)

    # Add custom shell script phases driving "make" for "rules" sections.
    #
    # Xcode's built-in rule support is almost powerful enough to use directly,
    # but there are a few significant deficiencies that render them unusable.
    # There are workarounds for some of its inadequacies, but in aggregate,
    # the workarounds added complexity to the generator, and some workarounds
    # actually require input files to be crafted more carefully than I'd like.
    # Consequently, until Xcode rules are made more capable, "rules" input
    # sections will be handled in Xcode output by shell script build phases
    # performed prior to the compilation phase.
    #
    # The following problems with Xcode rules were found.  The numbers are
    # Apple radar IDs.  I hope that these shortcomings are addressed, I really
    # liked having the rules handled directly in Xcode during the period that
    # I was prototyping this.
    #
    # 6588600 Xcode compiles custom script rule outputs too soon, compilation
    #         fails.  This occurs when rule outputs from distinct inputs are
    #         interdependent.  The only workaround is to put rules and their
    #         inputs in a separate target from the one that compiles the rule
    #         outputs.  This requires input file cooperation and it means that
    #         process_outputs_as_sources is unusable.
    # 6584932 Need to declare that custom rule outputs should be excluded from
    #         compilation.  A possible workaround is to lie to Xcode about a
    #         rule's output, giving it a dummy file it doesn't know how to
    #         compile.  The rule action script would need to touch the dummy.
    # 6584839 I need a way to declare additional inputs to a custom rule.
    #         A possible workaround is a shell script phase prior to
    #         compilation that touches a rule's primary input files if any
    #         would-be additional inputs are newer than the output.  Modifying
    #         the source tree - even just modification times - feels dirty.
    # 6564240 Xcode "custom script" build rules always dump all environment
    #         variables.  This is a low-prioroty problem and is not a
    #         show-stopper.
    rules_by_ext = {}
    for rule in spec.get('rules', []):
      rules_by_ext[rule['extension']] = rule

      # First, some definitions:
      #
      # A "rule source" is a file that was listed in a target's "sources"
      # list and will have a rule applied to it on the basis of matching the
      # rule's "extensions" attribute.  Rule sources are direct inputs to
      # rules.
      #
      # Rule definitions may specify additional inputs in their "inputs"
      # attribute.  These additional inputs are used for dependency tracking
      # purposes.
      #
      # A "concrete output" is a rule output with input-dependent variables
      # resolved.  For example, given a rule with:
      #   'extension': 'ext', 'outputs': ['$(INPUT_FILE_BASE).cc'],
      # if the target's "sources" list contained "one.ext" and "two.ext",
      # the "concrete output" for rule input "two.ext" would be "two.cc".  If
      # a rule specifies multiple outputs, each input file that the rule is
      # applied to will have the same number of concrete outputs.
      #
      # If any concrete outputs are outdated or missing relative to their
      # corresponding rule_source or to any specified additional input, the
      # rule action must be performed to generate the concrete outputs.

      # concrete_outputs_by_rule_source will have an item at the same index
      # as the rule['rule_sources'] that it corresponds to.  Each item is a
      # list of all of the concrete outputs for the rule_source.
      concrete_outputs_by_rule_source = []

      # concrete_outputs_all is a flat list of all concrete outputs that this
      # rule is able to produce, given the known set of input files
      # (rule_sources) that apply to it.
      concrete_outputs_all = []

      # actions is keyed by the same indices as rule['rule_sources'] and
      # concrete_outputs_by_rule_source.  It contains the action to perform
      # after resolving input-dependent variables.
      actions = []

      for rule_source in rule.get('rule_sources', []):
        rule_source_basename = os.path.basename(rule_source)
        (rule_source_root, rule_source_ext) = \
            os.path.splitext(rule_source_basename)

        # These are the same variable names that Xcode uses for its own native
        # rule support.  Because Xcode's rule engine is not being used, they
        # need to be expanded as they are written to the makefile.
        rule_input_dict = {
          'INPUT_FILE_BASE':   rule_source_root,
          'INPUT_FILE_SUFFIX': rule_source_ext,
          'INPUT_FILE_NAME':   rule_source_basename,
          'INPUT_FILE_PATH':   rule_source,
        }

        concrete_outputs_for_this_rule_source = []
        for output in rule.get('outputs', []):
          # Fortunately, Xcode and make both use $(VAR) format for their
          # variables, so the expansion is the only transformation necessary.
          # Any remaning $(VAR)-type variables in the string can be given
          # directly to make, which will pick up the correct settings from
          # what Xcode puts into the environment.
          concrete_output = ExpandXcodeVariables(output, rule_input_dict)
          concrete_outputs_for_this_rule_source.append(concrete_output)

          # Add all concrete outputs to the project.
          pbxp.AddOrGetFileInRootGroup(concrete_output)

        concrete_outputs_by_rule_source.append( \
            concrete_outputs_for_this_rule_source)
        concrete_outputs_all.extend(concrete_outputs_for_this_rule_source)
        if rule.get('process_outputs_as_sources', False):
          for output in concrete_outputs_for_this_rule_source:
            AddSourceToTarget(output, pbxp, xct)

        # Turn the list into a string that can be passed to a shell.
        action_string = gyp.common.EncodePOSIXShellList(rule['action'])

        action = ExpandXcodeVariables(action_string, rule_input_dict)
        actions.append(action)

      if len(concrete_outputs_all) > 0:
        # TODO(mark): There's a possibilty for collision here.  Consider
        # target "t" rule "A_r" and target "t_A" rule "r".
        makefile_name = '%s_%s.make' % (target_name, rule['rule_name'])
        makefile_path = os.path.join(xcode_projects[build_file].path,
                                     makefile_name)
        # TODO(mark): try/close?  Write to a temporary file and swap it only
        # if it's got changes?
        makefile = open(makefile_path, 'w')

        # make will build the first target in the makefile by default.  By
        # convention, it's called "all".  List all (or at least one)
        # concrete output for each rule source as a prerequisite of the "all"
        # target.
        makefile.write('all: \\\n')
        for concrete_output_index in \
            xrange(0, len(concrete_outputs_by_rule_source)):
          # Only list the first (index [0]) concrete output of each input
          # in the "all" target.  Otherwise, a parallel make (-j > 1) would
          # attempt to process each input multiple times simultaneously.
          # Otherwise, "all" could just contain the entire list of
          # concrete_outputs_all.
          concrete_output = \
              concrete_outputs_by_rule_source[concrete_output_index][0]
          if concrete_output_index == len(concrete_outputs_by_rule_source) - 1:
            eol = ''
          else:
            eol = ' \\'
          makefile.write('    %s%s\n' % (concrete_output, eol))

        for (rule_source, concrete_outputs, action) in \
            zip(rule['rule_sources'], concrete_outputs_by_rule_source, actions):
          makefile.write('\n')

          # Add a rule that declares it can build each concrete output of a
          # rule source.  Collect the names of the directories that are
          # required.
          concrete_output_dirs = []
          for concrete_output_index in xrange(0, len(concrete_outputs)):
            concrete_output = concrete_outputs[concrete_output_index]
            if concrete_output_index == 0:
              bol = ''
            else:
              bol = '    '
            makefile.write('%s%s \\\n' % (bol, concrete_output))

            concrete_output_dir = os.path.dirname(concrete_output)
            if not concrete_output_dir in concrete_output_dirs:
              concrete_output_dirs.append(concrete_output_dir)

          makefile.write('    : \\\n')

          # The prerequisites for this rule are the rule source itself and
          # the set of additional rule inputs, if any.
          prerequisites = [rule_source]
          prerequisites.extend(rule.get('inputs', []))
          for prerequisite_index in xrange(0, len(prerequisites)):
            prerequisite = prerequisites[prerequisite_index]
            if prerequisite_index == len(prerequisites) - 1:
              eol = ''
            else:
              eol = ' \\'
            makefile.write('    %s%s\n' % (prerequisite, eol))

          # Make sure that output directories exist before executing the rule
          # action.
          # TODO(mark): quote the list of concrete_output_dirs.
          if len(concrete_output_dirs) > 0:
            makefile.write('\tmkdir -p %s\n' % ' '.join(concrete_output_dirs))

          # The rule action has already had the necessary variable
          # substitutions performed.
          makefile.write('\t%s\n' % action)

        makefile.close()

        # It might be nice to ensure that needed output directories exist
        # here rather than in each target in the Makefile, but that wouldn't
        # work if there ever was a concrete output that had an input-dependent
        # variable anywhere other than in the leaf position.

        # Don't declare any inputPaths or outputPaths.  If they're present,
        # Xcode will provide a slight optimization by only running the script
        # phase if any output is missing or outdated relative to any input.
        # Unfortunately, it will also assume that all outputs are touched by
        # the script, and if the outputs serve as files in a compilation
        # phase, they will be unconditionally rebuilt.  Since make might not
        # rebuild everything that could be declared here as an output, this
        # extra compilation activity is unnecessary.  With inputPaths and
        # outputPaths not supplied, make will always be called, but it knows
        # enough to not do anything when everything is up-to-date.
        script = \
"""exec "${DEVELOPER_BIN_DIR}/make" -f "${PROJECT_FILE_PATH}/%s" -j "$(sysctl -n hw.ncpu)"
exit 1
""" % makefile_name
        ssbp = gyp.xcodeproj_file.PBXShellScriptBuildPhase({
              'name': 'Rule "' + rule['rule_name'] + '"',
              'shellScript': script,
              'showEnvVarsInLog': 0,
            })

        # TODO(mark): this assumes too much knowledge of the internals of
        # xcodeproj_file; some of these smarts should move into xcodeproj_file
        # itself.
        xct._properties['buildPhases'].insert(prebuild_index, ssbp)
        prebuild_index = prebuild_index + 1

      # Extra rule inputs also go into the project file.  Concrete outputs were
      # already added when they were computed.
      for group in ['inputs', 'inputs_excluded']:
        for item in rule.get(group, []):
          pbxp.AddOrGetFileInRootGroup(item)

    # Add "sources".
    for source in spec.get('sources', []):
      (source_root, source_extension) = os.path.splitext(source)
      if source_extension not in rules_by_ext:
        # AddSourceToTarget will add the file to a root group if it's not
        # already there.
        AddSourceToTarget(source, pbxp, xct)
      else:
        pbxp.AddOrGetFileInRootGroup(source)

    # TODO(mark): Hard-coded to "application" now, but this should match any
    # bundled type.
    if spec['type'] == 'application':
      # Add "mac_bundle_resources".
      for resource in spec.get('mac_bundle_resources', []):
        AddResourceToTarget(resource, pbxp, xct)

    # Excluded files can also go into the project file.
    for key in ['sources', 'mac_bundle_resources']:
      excluded_key = key + '_excluded'
      for item in spec.get(excluded_key, []):
        pbxp.AddOrGetFileInRootGroup(item)

    # So can "inputs" and "outputs" sections of "actions" groups.
    for action in spec.get('actions', []):
      groups = ['inputs', 'inputs_excluded', 'outputs', 'outputs_excluded']
      for group in groups:
        for item in action.get(group, []):
          # Exclude anything in BUILT_PRODUCTS_DIR.  They're products, not
          # sources.
          if not item.startswith('$(BUILT_PRODUCTS_DIR)/'):
            pbxp.AddOrGetFileInRootGroup(item)

    for postbuild in spec.get('postbuilds', []):
      action_string_sh = gyp.common.EncodePOSIXShellList(postbuild['action'])
      script = 'exec ' + action_string_sh + '\nexit 1\n'
      ssbp = gyp.xcodeproj_file.PBXShellScriptBuildPhase({
            'name': 'Postbuild "' + postbuild['postbuild_name'] + '"',
            'shellScript': script,
            'showEnvVarsInLog': 0,
          })
      xct.AppendProperty('buildPhases', ssbp)

    # Add dependencies before libraries, because adding a dependency may imply
    # adding a library.  It's preferable to keep dependencies listed first
    # during a link phase so that they can override symbols that would
    # otherwise be provided by libraries, which will usually include system
    # libraries.  On some systems, ld is finicky and even requires the
    # libraries to be ordered in such a way that unresolved symbols in
    # earlier-listed libraries may only be resolved by later-listed libraries.
    # The Mac linker doesn't work that way, but other platforms do, and so
    # their linker invocations need to be constructed in this way.  There's
    # no compelling reason for Xcode's linker invocations to differ.

    if 'dependencies' in spec:
      for dependency in spec['dependencies']:
        xct.AddDependency(xcode_targets[dependency])

    if 'libraries' in spec:
      for library in spec['libraries']:
        xct.FrameworksPhase().AddFile(library)
        # Add the library's directory to LIBRARY_SEARCH_PATHS if necessary.
        # I wish Xcode handled this automatically.
        # TODO(mark): this logic isn't right.  There are certain directories
        # that are always searched, we should check to see if the library is
        # in one of those directories, and if not, we should do the
        # AppendBuildSetting thing.
        if not os.path.isabs(library) and not library.startswith('$'):
          # TODO(mark): Need to check to see if library_dir is already in
          # LIBRARY_SEARCH_PATHS.
          library_dir = os.path.dirname(library)
          xct.AppendBuildSetting('LIBRARY_SEARCH_PATHS', library_dir)

    for configuration_name in configuration_names:
      configuration = spec['configurations'][configuration_name]
      xcbc = xct.ConfigurationNamed(configuration_name)
      for include_dir in configuration.get('mac_framework_dirs', []):
        xcbc.AppendBuildSetting('FRAMEWORK_SEARCH_PATHS', include_dir)
      for include_dir in configuration.get('include_dirs', []):
        xcbc.AppendBuildSetting('HEADER_SEARCH_PATHS', include_dir)
      if 'defines' in configuration:
        for define in configuration['defines']:
          # If the define is of the form A="B", escape the quotes yielding
          # A=\"B\".
          set_define = re.sub('^([^=]*=)"([^"]*)"$', '\\1\\"\\2\\"', define)
          xcbc.AppendBuildSetting('GCC_PREPROCESSOR_DEFINITIONS', set_define)
      if 'xcode_settings' in configuration:
        for xck, xcv in configuration['xcode_settings'].iteritems():
          xcbc.SetBuildSetting(xck, xcv)

  build_files = []
  for build_file, build_file_dict in data.iteritems():
    if build_file.endswith('.gyp'):
      build_files.append(build_file)

  for build_file in build_files:
    xcode_projects[build_file].Finalize1(xcode_targets, data[build_file])

  for build_file in build_files:
    xcode_projects[build_file].Finalize2(xcode_targets)

  for build_file in build_files:
    xcode_projects[build_file].Write()
