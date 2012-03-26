# -*- coding: utf-8 -*-
import os, re, logging
from inspect import ismethod, isfunction
from AccessControl.PermissionRole import rolesForPermissionOn
from AccessControl.SecurityManagement import noSecurityManager
from ConfigParser import ParsingError, NoOptionError
from BeautifulSoup import BeautifulSoup
from DateTime import DateTime
from urllib import unquote
from HTMLParser import HTMLParseError
from urlparse import urlsplit, urlparse

from zope.component import getMultiAdapter, queryMultiAdapter, getAdapters
try:
    from zope.app.publisher.interfaces import IResource
except ImportError:
    from zope.component.interfaces import IResource
from zope.contentprovider.interfaces import ContentProviderLookupError
from zope.publisher.interfaces import NotFound
from zope.publisher.interfaces.browser import IDefaultBrowserLayer
from zope.publisher.browser import applySkin

from OFS.Image import Pdata, File, Image as OFSImage
from plone.app.blob.content import ATBlob
from plone.app.blob.interfaces import IBlobImageField, IBlobField
from Products.Archetypes.Field import Image as ImageField
from Products.ATContentTypes.content.image import ATImage
from Products.Archetypes.interfaces import IBaseObject
from Products.CMFCore.FSDTMLMethod import FSDTMLMethod
from Products.CMFCore.FSFile import FSFile
from Products.CMFCore.FSImage import FSImage
from Products.CMFCore.FSPageTemplate import FSPageTemplate
from Products.CMFCore.utils import getToolByName
from Products.CMFPlone import PloneMessageFactory as _
from Products.CMFPlone.Portal import PloneSite
from Products.Five import BrowserView
from Products.PythonScripts.PythonScript import PythonScript
from Products.statusmessages.interfaces import IStatusMessage

from stxnext.staticdeployment.browser.preferences.staticdeployment import IStaticDeployment
from stxnext.staticdeployment.interfaces import ITransformation, IDeploymentStep, IExtraDeploymentCondition, \
    IPostTransformation, IImageTransformation
from stxnext.staticdeployment.utils import ConfigParser, get_config_path


log = logging.getLogger(__name__)


RE_WO_1ST_DIRECTORY = re.compile(r'^[^/]+?[/](.*)$')
RE_CSS_URL = re.compile(r"""url\(["']?([^\)'"]+)['"]?\)""")
RE_CSS_IMPORTS = re.compile(r"(?<=url\()[a-zA-Z0-9\.\-\/\:\_]+\.(?:css)")
RE_NOT_BINARY = re.compile(r'\.css$|\.js$|\.txt$|\.html$')


def _makedirs(path):
    try:
        os.makedirs(os.path.normpath(path))
    except OSError:
        return False
    return True


class StaticDeploymentUtils(object):
    """
    View for static deployment.
    """
    
    def _apply_request_modifications(self, section):
        """
        Apply proper skin name and five skinlayer. 
        """
        config_path = os.path.normpath(get_config_path())
        skins_tool = getToolByName(self.context, 'portal_skins')
        request_varname = skins_tool.request_varname
        self._read_config(config_path, section)

        layer_interface_path = self.layer_interface.split('.')
        layer_interface_module = __import__('.'.join(layer_interface_path[:-1]), {}, {}, layer_interface_path[-1])
        applySkin(self.request, getattr(layer_interface_module, layer_interface_path[-1], None))
        self.context.changeSkin(self.defaultskin_name, self.request)
        self.request.set(request_varname, self.defaultskin_name)

        self.base_dir = os.path.normpath(self.deployment_directory)
        self.deployed_resources = []
        
    def revert_request_modifications(self, context, request):
        """
        Apply plone default skin name and five skinlayer.
        """
        skins_tool = getToolByName(context, 'portal_skins')
        request_varname = skins_tool.request_varname
        applySkin(request, IDefaultBrowserLayer)
        context.changeSkin(None, request)
        request.set(request_varname, None)

    def _read_config(self, config_path, section):
        """
        Read config from .ini file.
        """
        config_file = open(config_path, 'r')
        self.config = ConfigParser()
        try:
            self.config.readfp(config_file)
        except ParsingError, e:
            log.exception("Error when trying to parse '%s'" % config_path)
            return

        # non required params
        self.page_types = self.config.get_as_list('page-types', section=section)
        self.file_types = self.config.get_as_list('file-types', section=section)
        self.skinstool_files = self.config.get_as_list('skinstool-files', section=section)
        self.additional_files = self.config.get_as_list('additional-files', section=section)
        self.additional_pages = self.config.get_as_list('additional-pages', section=section)
        self.deployment_steps = self.config.get_as_list('deployment-steps', section=section)
        self.additional_directories = self.config.get_as_list('additional-directories', section=section)

        try:
            self.relative_links = self.config.getboolean(section,
                    'make-links-relative')
        except NoOptionError:
            self.relative_links = False

        try:
            self.add_index = self.config.getboolean(section, 'add-index')
        except NoOptionError:
            self.add_index = False

        # required params
        try:
            self.deploy_plonesite = self.config.getboolean(section, 'deploy-plonesite')
        except NoOptionError:
            self.deploy_plonesite = True

        try:
            self.deploy_registry_files = self.config.getboolean(section, 'deploy-registry-files')
        except NoOptionError:
            self.deploy_registry_files = True

        self.deployable_review_states = self.config.get_as_list('deployable-review-states', section=section)
        if not self.deployable_review_states:
            self.deployable_review_states = ['published']

        try:
            self.deployment_directory = self.config.get(section, 'deployment-directory').strip()
            self.layer_interface = self.config.get(section, 'layer-interface').strip()
            self.defaultskin_name = self.config.get(section, 'defaultskin-name').strip()
        except NoOptionError, e:
            messages = IStatusMessage(self.request)
            messages.addStatusMessage(_(e.message), type='error')
            raise e


    def _apply_transforms(self, html):
        """
        Apply transforms to output html.
        """
        transformations = getAdapters((self.context,), ITransformation)

        for t_name, t in transformations:
            html = t(html)
        return html

    def _apply_post_transforms(self, html, file_path=None):
        """
        Apply post transforms to output html.
        """
        transformations = getAdapters((self.context,), IPostTransformation)

        for t_name, t in transformations:
            html = t(html, file_path)
        return html

    def _apply_image_transforms(self, filename, image):
        """
        Apply transforms to output image.
        """
        transformations = getAdapters((self.context,), IImageTransformation)
        
        for t_name, t in transformations:
            filename, image = t(filename, image)
        return filename, image

    def _parse_date(self, last_triggered):
        """
        Parse modification date passed in request.
        """
        if not last_triggered:
            return None
        try:
            last_triggered_date = DateTime(last_triggered)
            if last_triggered_date.isFuture():
                raise DateTime.DateError
        except (SyntaxError, DateTime.DateError), e:
            messages = IStatusMessage(self.request)
            message = _(u'Wrong format of last static deployment date.')
            messages.addStatusMessage(message, type='error')
            raise e
        
        return last_triggered_date

    def initial_resources_tools_mode(self, context):
        """
        Set debug mode for css and js tools and returns initial values
        """
        css_tool = getToolByName(context, 'portal_css')
        js_tool = getToolByName(context, 'portal_javascripts')
        kss_tool = getToolByName(context, 'portal_kss')
        initial_debugmode = css_tool.getDebugMode(), js_tool.getDebugMode(), kss_tool.getDebugMode() 
        if initial_debugmode[0]: css_tool.setDebugMode(False)
        if initial_debugmode[1]: js_tool.setDebugMode(False)
        if initial_debugmode[2]: kss_tool.setDebugMode(False)
        return initial_debugmode
    
    def revert_resources_tools_mode(self, context, initial_debugmode=(True, True)):
        """
        Set initial mode for css and js tools.
        """
        css_tool = getToolByName(context, 'portal_css')
        js_tool = getToolByName(context, 'portal_javascripts')
        kss_tool = getToolByName(context, 'portal_kss')
        if initial_debugmode[0]: css_tool.setDebugMode(True)
        if initial_debugmode[1]: js_tool.setDebugMode(True)
        if initial_debugmode[2]: kss_tool.setDebugMode(True)


    def deploy(self, context, request, section, last_triggered=None):
        """
        Deploy whole site as static content.
        """
        # get content for Anonymous users, not authenticated
        noSecurityManager()
        self.context = context
        self.request = request
        self.section = section
        self._apply_request_modifications(section)

        modification_date = self._parse_date(last_triggered)

        ## Deploy registry files
        if self.deploy_registry_files:
            self._deploy_registry_files('portal_css', 'styles', 'styles')
            self._deploy_registry_files('portal_javascripts', 'scripts', 'scripts')
            self._deploy_registry_files('portal_kss', 'kss', 'kineticstylesheets')

        self._deploy_skinstool_files(self.skinstool_files)
        self._deploy_views(self.additional_files, is_page=False)
        self._deploy_views(self.additional_pages, is_page=True)

        ## Deploy Plone Site
        if self.deploy_plonesite:
            self._deploy_site(self.context)

        ## Deploy folders and pages
        catalog = getToolByName(self.context, 'portal_catalog')
        brains = catalog(meta_type=self.page_types + self.file_types,
                         modified={'query':[modification_date], 'range':'min'},
                         effectiveRange = DateTime(),
                         )
        for brain in brains:
            if not brain.review_state or brain.review_state in self.deployable_review_states:
                obj = brain.getObject()
                chain = obj.aq_chain
                exclude = False
                for subobj in chain:
                    if IBaseObject.providedBy(subobj) or isinstance(subobj, PloneSite):
                        if not 'Anonymous' in rolesForPermissionOn('View', subobj):
                            exclude = True
                            break

                # extra deployment conditions
                if not exclude:
                    extra_dep_conds = getAdapters((self.context,), IExtraDeploymentCondition)
                    for cond_name, condition in extra_dep_conds:
                        condition.update(self, modification_date)
                        if not condition(obj):
                            exclude = True
                            break

                if not exclude:
                    if brain.meta_type in self.page_types:
                        is_page = True
                    else:
                        is_page = False
                    self._deploy_content(obj, is_page=is_page)

        ## find and run additional deployment steps
        steps = getAdapters((self.context,), IDeploymentStep)
        for step_name, step in steps:
            if step_name in self.deployment_steps:
                step.update(self, modification_date)
                step()

        settings = IStaticDeployment(self.context)
        settings.last_triggered = unicode(DateTime().strftime('%Y/%m/%d %H:%M:%S'))


    def _deploy_registry_files(self, registry_type, resource_name, resource_type):
        """
        Deploy registered resources.
        """
        registry_view = getMultiAdapter((self.context, self.request), name='resourceregistries_%s_view' % resource_name)
        registry = registry_view.registry()
        resources = getattr(registry_view, resource_type)()

        for resource in resources:
            filename = urlparse(resource['src']).path
            try:
                content = registry.getResourceContent(os.path.basename(filename), self.context)
            except TypeError:
                log.exception("File '%s' not found when deploying '%s'!" % (filename, registry_type))
                continue

            self._write(filename, content)

    def _deploy_skinstool_files(self, files):
        """
        Deploy files from portal_skins but not registered in portal_css or portal_js.
        """
        skins_tool = getToolByName(self.context, 'portal_skins')

        for fs_file_path in files:
            fs_file = skins_tool.getSkinByPath(fs_file_path)
            if not fs_file:
                log.warning("File '%s' not found in portal_skins!" % fs_file_path)
                continue

            filename = fs_file_path
            match = RE_WO_1ST_DIRECTORY.match(filename)
            if match:
                filename = match.group(1)

            content = fs_file._readFile(None)
            
            path = urlparse(self.context.portal_url()).path
            filename = '/'.join((path, filename))
            
            if isinstance(fs_file, FSImage):
                filename, content = self._apply_image_transforms(filename, content)
            self._write(filename, content)

    def _deploy_views(self, views, is_page=False):
        """
        Deploy views of context as pages.
        """
        for fullview_name in views:
            
            fullview_path = None
            fullview_name_args = fullview_name.split('|')
            if len(fullview_name_args) > 1:
                fullview_name = fullview_name_args[0]
                fullview_path = fullview_name_args[1]
            
            context = self.context
            context_path = os.path.dirname(fullview_name)
            view_name = os.path.basename(fullview_name)
            if context_path:
                context = self.context.restrictedTraverse(context_path, None)
                if not context:
                    log.warning("Unable traverse to '%s'!" % context_path)
                    continue

            content_obj = context.restrictedTraverse(view_name, None)
            # get object's view content
            if ismethod(content_obj) or isfunction(content_obj):
                view = queryMultiAdapter((context, self.request), name=view_name)
                content_obj = view.context()
            content = self._render_obj(content_obj)
            if content is None:
                continue

            filename = fullview_name
            if is_page:
                filename = os.path.join(filename, 'index.html')
                
            path = urlparse(self.context.portal_url()).path
            filename = '/'.join((path, filename))
            self._write(filename, content, fullview_path)

    def _render_obj(self, obj):
        """
        Render object to string.
        """
        if isinstance(obj, basestring):
            return obj
        ## 'plone.global_sections' viewlet uses request['URL'] highlight
        ## selected tab, so it must be overridden but only for a while
        initial_url = self.request['URL']
        try:
            obj_url = obj.absolute_url()
        except AttributeError:
            try:
                obj_url = obj.context.absolute_url()
            except AttributeError:
                obj_url = None

        if obj_url:
            self.request['URL'] = obj_url

        ## breadcrumb implementation in quills uses 'PARENTS', so it must
        ## be overriden but ony for a while 
        initial_parents = self.request['PARENTS']
        self.request['PARENTS'] = obj.aq_chain

        try:
            if IResource.providedBy(obj):
                try:
                    f = open(obj.context.path)
                    result = f.read()
                    f.close()
                except IOError:
                    log.error("Couldn't open '%s' file with resource" % obj.context.path)
                    return None

                return result

            if isinstance(obj, (BrowserView, FSPageTemplate, PythonScript)):
                try:
                    return obj()
                except NotFound:
                    log.error("Resource '%s' not found" % repr(obj))
                    return None
            if isinstance(obj, (FSFile, FSImage)):
                return self._render_obj(obj._readFile(None))

            if isinstance(obj, FSDTMLMethod):
                return self._render_obj(obj.read())

            mt = None
            try:
                mt = obj.aq_base.meta_type
            except AttributeError:
                pass

            if mt in self.file_types or isinstance(obj, (ImageField, OFSImage, Pdata, File)):
                return self._render_obj(obj.data)

            if IBaseObject.providedBy(obj) or isinstance(obj, PloneSite):
                def_page_id = obj.getDefaultPage()
                if def_page_id:
                    def_page = obj[def_page_id]
                    return self._render_obj(def_page)

                view_name = obj.getLayout()
                view = queryMultiAdapter((obj, self.request), name=view_name)
                if view:
                    try:
                        return view.context()
                    except ContentProviderLookupError:
                        pass

                view = obj.restrictedTraverse(view_name, None)
                if view:
                    try:
                        return view.context()
                    except AttributeError:
                        view()

                try:
                    return obj()
                except AttributeError:
                    pass

        finally:
            ## back to initial url
            if obj_url:
                self.request['URL'] = initial_url

            ## back to initial parents
            self.request['PARENTS'] = initial_parents
        
        log.warning("Not recognized object '%s'!" % repr(obj))
        return None

    def _deploy_site(self, obj):
        """
        Deploy object as page.
        """
        content = self._render_obj(obj)
        if content is None:
            return
        
        path = urlparse(self.context.portal_url()).path
        self._write('/'.join((path, 'index.html')), content)

    def _deploy_content(self, obj, is_page=True):
        """
        Deploy object as page.
        """
        content = self._render_obj(obj)
        if content is None:
            return

        filename = obj.absolute_url_path().lstrip('/')
        # deploy additional views for content type
        if isinstance(obj, ATBlob):
            self._deploy_views([os.path.join(filename, 'view'), ],
                    is_page=True)

        if is_page:
            filename = os.path.join(filename, 'index.html')
        elif isinstance(obj, ATImage) or hasattr(obj, 'getBlobWrapper') and 'image' in obj.getBlobWrapper().getContentType():
            # create path to dump ATImage in original size
            if filename.rsplit('.', 1)[-1] in ('png', 'jpg', 'gif', 'jpeg'):
                filename = os.path.join(filename, 'image.%s' % filename.rsplit('.', 1)[-1])
            else:
                filename = os.path.join(filename, 'image.jpg')
            filename, content = self._apply_image_transforms(filename, content)
        elif (hasattr(obj, 'getBlobWrapper') and 'image' not in
                obj.getBlobWrapper().getContentType()):
            # create path like for ATImage
            if len(filename.rsplit('.', 1)) > 1:
                filename  = os.path.join(filename, 'file.%s' % filename.rsplit('.', 1)[-1])
            else:
                filename = os.path.join(filename, 'file')

        self._write(filename, content)

        # deploy all sizes of images uploaded for the object
        if not getattr(obj, 'schema', None):
            return

        for field in obj.Schema().fields():
            if IBlobImageField.providedBy(field):
                sizes = field.getAvailableSizes(field)
                scalenames = sizes.keys()
                scalenames.append(None)
                for scalename in scalenames:
                    image =  field.getScale(obj, scale=scalename)
                    if image:
                        #store original image
                        if scalename is None:
                            filename = image.filename
                            image = image.data
                        else:
                            filename = image.getId()
                        dir_path = obj.absolute_url_path().lstrip('/')
                        if filename.rsplit('.', 1)[-1] in ('png', 'jpg', 'gif', 'jpeg'):
                            objpath = os.path.join(filename, 'image.%s' %
                                    filename.rsplit('.', 1)[-1])
                        else:
                            objpath = os.path.join(filename, 'image.jpg')

                        file_path = os.path.join(dir_path, objpath)
                        content = self._render_obj(image)
                        if content:
                            file_path, content = self._apply_image_transforms(file_path, content)
                            self._write(file_path, content)
                            # add as already deployed resource to avoid
                            # redeployment in _deploy_resources
                            self.deployed_resources.append(file_path)

            elif IBlobField.providedBy(field):
                file_instance = field.getAccessor(obj)()
                if file_instance:
                    filename = field.getName()
                    dir_path = obj.absolute_url_path().lstrip('/')
                    file_path = os.path.join(dir_path, 'at_download', filename)
                    if hasattr(file_instance, 'data'):
                        content = self._render_obj(str(file_instance.data))
                        if content:
                            self._write(file_path, content)
            else:
                continue

    def _deploy_resources(self, urls, base_path):
        """
        Deploy resources linked in HTML or CSS.
        """
        portal_url = getToolByName(self.context, 'portal_url')()
        for url in urls:
            url = url.strip()
            scheme, netloc, path, query, fragment = urlsplit(url)
            if not path:
                ## internal anchor
                continue

            if netloc and netloc != portal_url:
                ## external link
                continue

            if path.startswith('/'):
                objpath = path[1:]
            else:
                objpath = os.path.join(base_path, path)

            if isinstance(objpath, unicode):
                objpath = objpath.encode('utf-8')

            if objpath in self.deployed_resources:
                continue
            obj = self.context.restrictedTraverse(objpath, None)
            if objpath.rsplit('/', 1)[-1].split('.')[0] == 'image':
                obj = self.context.restrictedTraverse(objpath.rsplit('.', 1)[0], None)
            if not obj:
                obj = self.context.restrictedTraverse(unquote(objpath), None)
            if not obj:
                log.warning("Unable to deploy resource '%s'!" % objpath)
                continue
            if isinstance(obj, ATImage) or hasattr(obj, 'getBlobWrapper') and 'image' in obj.getBlobWrapper().getContentType():
                # create path to dump ATImage in original size
                if objpath.rsplit('.', 1)[-1] in ('png', 'jpg', 'gif', 'jpeg'):
                    objpath = os.path.join(objpath, 'image.%s' % objpath.rsplit('.', 1)[-1])
                else:
                    objpath = os.path.join(objpath, 'image.jpg')

            content = self._render_obj(obj)
            if content is None:
                continue

            if isinstance(obj, (FSImage, OFSImage, ATImage)) or hasattr(obj, 'getBlobWrapper') and \
                'image' in obj.getBlobWrapper().getContentType():
                objpath, content = self._apply_image_transforms(objpath, content)

            self._write(objpath, content)

            self.deployed_resources.append(objpath)

    def _parse_html(self, html, base_path=''):
        """
        Save all resources used in HTML file.
        """
        try:
            soup = BeautifulSoup(html)
        except HTMLParseError:
            return
        
        # deploying resources only from local domain (the path don't contain external address) 
        urls = [tag['src'] for tag in soup.findAll(['img', 'input', 'embed', 'script'], src=True) if not urlparse(tag['src'])[0]]
        css_imports = RE_CSS_IMPORTS.findall(html)
        css_imports = [link for link in css_imports if not urlparse(link)[0]]
        local_styles = RE_CSS_URL.findall(html)
        urls = urls + css_imports + local_styles
        self._deploy_resources(urls, unquote(base_path))

    def _parse_css(self, content, base_path=''):
        """
        Save all resources used in CSS file.
        """
        self._deploy_resources(RE_CSS_URL.findall(content), unquote(base_path))

    def _write(self, filename, content, dir_path=None, omit_transform=False):
        """
        Write content to file.
        """
        filename = filename.lstrip('/') 

        if not content:
            log.warning("File '%s' is empty." % filename)

        if dir_path is None:
            dir_path = self.base_dir
        file_path = os.path.join(dir_path, filename)
        file_path = unquote(file_path)
        _makedirs(os.path.dirname(file_path))

        try:
            content_file = open(file_path, "w")
        except IOError:
            log.exception("Error trying to dump data to '%s' file!" % filename)
            return

        if RE_NOT_BINARY.search(filename) and not omit_transform:
            pre_transformated_content = self._apply_transforms(content)
            post_transformated_content = self._apply_post_transforms(
                    pre_transformated_content, file_path)
        else:
            pre_transformated_content = post_transformated_content = content

        try:
            content_file.write(post_transformated_content)
        except UnicodeEncodeError:
            content_file.write(post_transformated_content.encode('utf-8'))
        finally:
            content_file.close()

        log.debug("[*] '%s' saved." % filename)

        if filename.endswith('.css'):
            self._parse_css(pre_transformated_content, os.path.dirname(filename))

        if filename.endswith('.html'):
            self._parse_html(pre_transformated_content, os.path.dirname(filename))
