"""
Plugin manager module that provides functionality to add, modify and delete plugins to the
plugins django app. There is also functionality to run and check the execution status of a
plugin app.
"""

import os
import sys
import json
import docker
import time
from argparse import ArgumentParser
import pudb

if "DJANGO_SETTINGS_MODULE" not in os.environ:
    # django needs to be loaded (eg. when this script is run from the command line)
    sys.path.append(os.path.join(os.path.dirname(__file__), '../../'))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    import django
    django.setup()

from django.utils import timezone
from plugins.models import Plugin, PluginParameter, TYPES, PLUGIN_TYPE_CHOICES, STATUS_TYPES
from plugins.services import charm


class PluginManager(object):

    def __init__(self):
        parser = ArgumentParser(description='Manage plugins')
        group = parser.add_mutually_exclusive_group()
        group.add_argument("-a", "--add", help="add a new plugin", metavar='DockImage')
        group.add_argument("-r", "--remove", help="remove an existing plugin",
                           metavar='PluginName')
        group.add_argument("-m", "--modify", help="register NOW as modification date",
                           metavar='DockImage')
        self.parser = parser

        self.str_service        = ''
        self.str_IOPhost        = ''
        # Debug specifications
        self.b_quiet            = False
        self.b_useDebug         = True
        self.str_debugFile      = '%s/tmp/debug-charm.log' % os.environ['HOME']

    def get_plugin_app_representation(self, dock_image_name):
        """
        Get a plugin app representation given its docker image name.
        """
        client = docker.from_env()
        # first try to pull the latest image
        try:
            img = client.images.pull(dock_image_name)
        except docker.errors.APIError:
            # use local image ('remove' option automatically removes container when finished)
            byte_str = client.containers.run(dock_image_name, remove=True)
        else:
            byte_str = client.containers.run(img, remove=True)
        app_repr = json.loads(byte_str.decode())
        plugin_types = [plg_type[0] for plg_type in PLUGIN_TYPE_CHOICES]
        if app_repr['type'] not in plugin_types:
            raise ValueError("A plugin's TYPE can only be any of %s. Please fix it in %s"
                             % (plugin_types, dock_image_name))
        return app_repr

    def get_plugin_name(self, app_repr):
        """
        Get a plugin app's name from the plugin app's representation.
        """
        # the plugin app exec name stored in 'selfexec' must be: 'plugin name' + '.py'
        if 'selfexec' not in app_repr:
            raise KeyError("Missing 'selfexec' from plugin app's representation")
        return app_repr['selfexec'].rsplit( ".", 1 )[ 0 ]

    def _save_plugin_param(self, plugin, param):
        """
        Internal method to save a plugin parameter into the DB.
        """
        # add plugin parameter to the db
        plugin_param = PluginParameter()
        plugin_param.plugin = plugin
        plugin_param.name = param['name']
        plg_type = param['type']
        plugin_param.type = [key for key in TYPES if TYPES[key]==plg_type][0]
        plugin_param.optional = param['optional']
        if param['default'] is None:
            plugin_param.default = ""
        else:
            plugin_param.default = str(param['default'])
        plugin_param.help = param['help']
        plugin_param.save()
        
    def add_plugin(self, dock_image_name):
        """
        Register/add a new plugin to the system.
        """
        # get representation from the corresponding app
        app_repr = self.get_plugin_app_representation(dock_image_name)
        name = self.get_plugin_name(app_repr)
        # check wether the plugin already exist
        existing_plugin_names = [plugin.name for plugin in Plugin.objects.all()]
        if name in existing_plugin_names:
            raise ValueError("Plugin '%s' already exists in the system" % name)
        # add plugin to the db
        plugin = Plugin()
        plugin.name = name
        plugin.dock_image = dock_image_name
        plugin.type = app_repr['type']
        plugin.save()
        # add plugin's parameters to the db
        params = app_repr['parameters']
        for param in params:
            self._save_plugin_param(plugin, param)

    def get_plugin(self, name):
        """
        Get an existing/registered plugin.
        """
        try:
            plugin = Plugin.objects.get(name=name)
        except Plugin.DoesNotExist:
            raise NameError("Couldn't find '%s' plugin in the system" % name)
        return plugin
                  
    def remove_plugin(self, name):
        """
        Remove an existing/registered plugin from the system.
        """
        plugin = self.get_plugin(name)
        plugin.delete()

    def register_plugin_app_modification(self, dock_image_name):
        """
        Register/add new parameters to a plugin from the corresponding plugin's app.
        Also add the current date as a new plugin modification date.
        """
        # get representation from the corresponding app
        app_repr = self.get_plugin_app_representation(dock_image_name)
        name = self.get_plugin_name(app_repr)
        plugin = self.get_plugin(name)
        new_params = app_repr['parameters']
        existing_param_names = [parameter.name for parameter in plugin.parameters.all()]
        for param in new_params:
            if param['name'] not in existing_param_names:
                self._save_plugin_param(plugin, param)
        plugin.modification_date = timezone.now()
        plugin.save()

    def run(self, args=None):
        """
        Parse the arguments passed to the manager and perform the appropriate action.
        """
        options = self.parser.parse_args(args)
        if options.add:
            self.add_plugin(options.add)
        elif options.remove:
            self.remove_plugin(options.remove)
        elif options.modify:
            self.register_plugin_app_modification(options.modify)
        self.args = options

    def run_plugin_app(self, plugin_inst, parameter_dict, **kwargs):
        """
        Run a plugin's app.
        """

        # These directory overrides allow for mapping from the original ChRIS dir space
        # to the plugin input and output dir spaces.
        str_inputDirOverride        = ''
        str_outputDirOverride       = ''

        for k, v in kwargs.items():
            if k == 'useDebug':             self.b_useDebug         = v
            if k == 'debugFile':            self.str_debugFile      = v
            if k == 'quiet':                self.b_quiet            = v
            if k == 'service':              self.str_service        = v
            if k == 'inputDirOverride':     str_inputDirOverride    = v
            if k == 'outputDirOverride':    str_outputDirOverride   = v
            if k == 'IOPhost':              self.str_IOPhost        = v

        plugin_repr = self.get_plugin_app_representation(plugin_inst.plugin.dock_image)
        # get input dir
        inputdir            = ""
        inputdirManagerFS   = ""
        if plugin_inst.previous:
            inputdirManagerFS   = plugin_inst.previous.get_output_path()
            inputdir            = inputdirManagerFS
        if len(str_inputDirOverride):
            inputdir = str_inputDirOverride
        # get output dir
        outputdirManagerFS      = plugin_inst.get_output_path()
        outputdir               = outputdirManagerFS
        if len(str_outputDirOverride):
            outputdir = str_outputDirOverride
        app_args = []
        # append input dir to app's argument list (only for ds plugins)
        if plugin_repr['type'] == 'ds' and inputdir:
            app_args.append(inputdir)
        # append output dir to app's argument list
        app_args.append(outputdir)
        # append flag to save input meta data (passed options)
        app_args.append("--saveinputmeta")
        # append flag to save output meta data (output description)
        app_args.append("--saveoutputmeta")
        # append the parameters to app's argument list
        if parameter_dict:
            for param_name in parameter_dict:
                param_value = parameter_dict[param_name]
                for plugin_param in plugin_repr['parameters']:
                    if plugin_param['name'] == param_name:
                        app_args.append(plugin_param['flag'])
                        if plugin_param['action'] == 'store':
                            app_args.append(param_value)
                        break

        # run the app via an external REST service...
        chris_service = charm.Charm(
            app_args    = app_args,
            d_args      = parameter_dict,
            plugin_inst = plugin_inst,
            plugin_repr = plugin_repr,
            inputdir    = inputdirManagerFS,
            outputdir   = outputdirManagerFS,
            IOPhost     = self.str_IOPhost
        )

        # Some dev notes...
        #
        # To run the app directly on the CLI via 'crunner', use
        #         chris_service.app_manage(method = 'crunner')
        # Note that this call blocks until the CLI process returns.
        #
        # To run the app 'internally', i.e. not as a CLI but by calling the app 
        # run() method directly in python:
        #         chris_service.app_manage(method = 'internal')
        # Again, this blocks on the app run() method.
        #
        # The call to "method = 'pfcon'" does not block but dispatches
        # to a completely asynchronous external server process and returns 
        # immediately. The check on output is performed when views are updated,
        # again by talking to the application server.
        #

        chris_service.app_manage(method     = self.str_service,
                                 IOPhost    = self.str_IOPhost   )

        # Finally, any output files generated by the app need to be registered in the DB.
        # This is handled by the app_manage() method, and in asynchronous cases (i.e. via the pman server)
        # registration occurs via a call to the overloaded retrieve() method in PluginInstanceDetail in
        # plugins/views.py.
        #

    def check_apps_exec_server(self, **kwargs):
        """
        check if a remote server instance is servicing requests
        :return:
        """
        # pudb.set_trace()
        chris_service   = charm.Charm(**kwargs)
        chris_service.app_service_checkIfAvailable(**kwargs)
        # Wait a bit for transients
        time.sleep(5)

    def shutdown_apps_exec_server(self, **kwargs):
        """
        Shutdown a remote server instance
        :return:
        """
        # pudb.set_trace()
        chris_service   = charm.Charm(**kwargs)
        chris_service.app_service_shutdown(**kwargs)
        # Wait a bit for transients
        time.sleep(5)

    def check_plugin_app_exec_status(self, plugin_inst, **kwargs):
        """
        Check a plugin's app execution status. It connects to the remote
        service to determine job status.
        """
        # pudb.set_trace()
        chris2service   = charm.Charm(
            plugin_inst = plugin_inst,
            **kwargs
        )
        chris2service.app_statusCheckAndRegister()        


# ENTRYPOINT
if __name__ == "__main__":
    manager = PluginManager()
    manager.run()


