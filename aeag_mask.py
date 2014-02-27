﻿"""
/***************************************************************************
Name                  : Mask 
Description          : Aide à la création de masque
Date                 : Feb/12 
copyright            : (C) 2011 by AEAG
                       (c) 2014 Oslandia
email                : xavier.culos@eau-adour-garonne.fr 
                       hugo.mercier@oslandia.com
todo: 

 add help file
 let's user change user defined parameters. 
 use a qml file to set masqk style. default qml inside plugin ? 
   Parameter 1: choose memory output or shp output + directory (default HOME directory)
   Parameter 2 : choose a qml file. Qml should be parsed to check if qymbology apply to polygon features

 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""

# Import the PyQt and QGIS libraries
import os
from PyQt4.QtCore import * 
from PyQt4.QtGui import *
from qgis.core import *
# for user-defined functions
from qgis.utils import qgsfunction

from maindialog import MainDialog
from layerlist import LayerListDialog
from mask_filter import *
from mask_parameters import *

# Initialize Qt resources from file resources.py
import resources_rc

_fromUtf8 = lambda s: (s.decode("utf-8").encode("latin-1")) if s else s
_toUtf8 = lambda s: s.decode("latin-1").encode("utf-8") if s else s

class MaskGeometryFunction( QgsExpression.Function ):
    def __init__( self, mask ):
        QgsExpression.Function.__init__( self, "$mask_geometry", 0, "Python", "Help" )
        self.mask = mask

    def func( self, values, feature, parent ):
        return self.mask.mask_geometry()

class MaskComplementGeometryFunction( QgsExpression.Function ):
    def __init__( self, mask ):
        QgsExpression.Function.__init__( self, "$mask_complement_geometry", 0, "Python", "Help" )
        self.mask = mask

    def func( self, values, feature, parent ):
        return self.mask.mask_complement_geometry()

class aeag_mask:

    def __init__(self, iface):
        # Save reference to the QGIS interface
        self.iface = iface
        self.toolBar = None
        self.act_aeag_mask = None
        self.act_aeag_toolbar_help = None
        self.canvas = self.iface.mapCanvas()

        # mask parameters
        self.parameters = MaskParameters()

        # mask layer
        self.layer = None
        self.is_memory_layer = True
        self.atlas_layer = None

        self.composers = {}

        # test qgis version for the presence of signals
        self.has_atlas_signals = 'renderBegun' in dir(QgsAtlasComposition)

        # Part of the hack to circumvent layers opened from MemoryLayerSaver
        self.must_reload_from_layer = None

    def initGui(self):  
        self.mask_geometry_function = MaskGeometryFunction( self )
        QgsExpression.registerFunction( self.mask_geometry_function )
        self.mask_complement_geometry_function = MaskComplementGeometryFunction( self )
        QgsExpression.registerFunction( self.mask_complement_geometry_function )

        #
        self.disable_remove_mask_signal = False
        self.registry = QgsMapLayerRegistry.instance()
        self.registry.layerWasAdded.connect( self.on_add_layer )
        self.registry.layerWillBeRemoved.connect( self.on_remove_mask )

        self.toolBar = self.iface.pluginToolBar()
        
        self.act_aeag_mask = QAction(QIcon(":plugins/mask/aeag_mask.png"), _fromUtf8("Create mask"), self.iface.mainWindow())
        self.toolBar.addAction(self.act_aeag_mask)
        
        self.iface.addPluginToMenu("&Mask", self.act_aeag_mask)    
        
        # Add actions to the toolbar
        self.act_aeag_mask.triggered.connect(self.run)
        
        # look for existing mask layer
        for name, layer in self.registry.mapLayers().iteritems():
            self.on_add_layer( layer )

        if not self.has_atlas_signals:
            print "no atlas signal"

        if self.has_atlas_signals:
            # register composer signals
            self.iface.composerAdded.connect( self.on_composer_added )
            self.iface.composerWillBeRemoved.connect( self.on_composer_removed )

            # register already existing composers
            for compo in self.iface.activeComposers():
                self.on_composer_added( compo )

    def unload(self):
        self.toolBar.removeAction(self.act_aeag_mask)
        self.iface.removePluginMenu("&Mask", self.act_aeag_mask)
        QgsExpression.unregisterFunction( "$mask_geometry" )
        QgsExpression.unregisterFunction( "$mask_complement_geometry" )

        self.registry.layerWasAdded.disconnect( self.on_add_layer )
        self.registry.layerWillBeRemoved.disconnect( self.on_remove_mask )

        if self.has_atlas_signals:
            self.iface.composerAdded.disconnect( self.on_composer_added )
            self.iface.composerWillBeRemoved.disconnect( self.on_composer_removed )
            # remove composer signals
            for compo in self.iface.activeComposers():
                self.on_composer_removed( compo )

    def on_composer_added( self, compo ):
        composition = compo.composition()
        self.composers[composition] = []
        print "on_composer_added", composition
        items = composition.composerMapItems()
        composition.atlasComposition().renderBegun.connect( self.on_atlas_begin_render )
        composition.atlasComposition().renderEnded.connect( self.on_atlas_end_render )

        composition.composerMapAdded.connect( lambda item: self.on_composer_map_added(composition, item) )
        composition.itemRemoved.connect( lambda item: self.on_composer_item_removed(composition,item) )
        for item in items:
            print item
            if item.type() == QgsComposerItem.ComposerMap:
                self.on_composer_map_added( composition, item )

    def on_composer_map_added( self, compo, item ):
        print "on_composer_map_added", item
        # The second argument, which is supposed to be a QgsComposerMap is always a QObject.
        # ?! So we circumvent this problem in passing the QgsComposition container
        # and getting track of composer maps
        for composer_map in compo.composerMapItems():
            if composer_map not in self.composers[compo]:
                print "new composer map", composer_map
                self.composers[compo].append(composer_map)
                composer_map.preparedForAtlas.connect( lambda : self.on_prepared_for_atlas(composer_map) )
                break

    def on_composer_item_removed( self, compo, _ ):
        for composer_map in self.composers[compo]:
            if composer_map not in compo.composerMapItems():
                print "remove composer map", composer_map
                self.composers[compo].remove(composer_map)
                composer_map.preparedForAtlas.disconnect()
                break

    def on_composer_removed( self, compo ):
        print "on_composer removed"
        composition = compo.composition()
        items = composition.composerMapItems()
        composition.atlasComposition().renderBegun.disconnect( self.on_atlas_begin_render )
        composition.atlasComposition().renderEnded.disconnect( self.on_atlas_end_render )
        composition.composerMapAdded.disconnect()
        composition.itemRemoved.disconnect()
        for item in items:
            self.on_composer_item_removed( composition, item )
        del self.composers[composition]

    def compute_mask_geometries( self, poly, extent ):
        dest_crs = self.canvas.mapRenderer().destinationCrs()
        geom = self.get_final_geometry( poly, dest_crs, self.parameters.do_simplify, self.parameters.simplify_tolerance )

        if self.parameters.do_buffer:
            geom = geom.buffer( self.parameters.buffer_units, self.parameters.buffer_segments )

        rgeometry = geom

        # build the complement geometry
        extent.scale(2)
        mask = QgsGeometry.fromRect( extent )
        rcomplement_geometry = mask.difference( geom )

        return rgeometry, rcomplement_geometry

    def on_prepared_for_atlas( self, item ):
        print "prepared for atlas", item
        if not self.atlas_layer:
            return

        atlas_layer = item.composition().atlasComposition().coverageLayer()
        geom = QgsExpression.specialColumn("$atlasgeometry")
        crs = atlas_layer.crs()
        fet = QgsFeature()
        fet.setGeometry(geom)
        extent = item.currentMapExtent()
        self.parameters.geometry, self.parameters.complement_geometry = self.compute_mask_geometries( [(fet,crs)], extent )
        self.parameters.save_to_layer( self.atlas_layer )
#        save_geom = self.complement_geometry if self.mask_mode == 'mask' else self.geometry
#        self.update_layer( self.atlas_layer, save_geom )

    def on_atlas_begin_render( self ):
        print "atlas begin render"
        if not self.layer:
            return
        if not self.atlas_layer:
            # add a memory layer for atlas
            dest_crs = self.canvas.mapRenderer().destinationCrs()
            self.atlas_layer = QgsVectorLayer("MultiPolygon?crs=%s" % dest_crs.authid(), "Mask_temp", "memory")
            self.copy_layer_style( self.layer, self.atlas_layer )
            self.registry.addMapLayer( self.atlas_layer )

            # insert it in place of the current 'mask' layer
            ll = self.iface.mapCanvas().mapRenderer().layerSet()
            ll.remove(self.atlas_layer.id())
            p = ll.index(self.layer.id())
            ll = ll[0:p] + [self.atlas_layer.id()] + ll[p:]
            print ll
            self.iface.mapCanvas().mapRenderer().setLayerSet(ll)

            # make the 'mask' layer not visible
            self.iface.legendInterface().setLayerVisible( self.layer, False )
        self.geometries_backup = (self.parameters.geometry, self.parameters.complement_geometry)
 
    def on_atlas_end_render( self ):
        print "atlas end render"
        if not self.atlas_layer:
            return

        # remove the atlas layer
        # restore the mask layer's visibility
        self.registry.removeMapLayer( self.atlas_layer.id() )
        self.atlas_layer = None
        self.parameters.geometry, self.parameters.complement_geometry = self.geometries_backup
        self.iface.legendInterface().setLayerVisible( self.layer, True )

    # run method that performs all the real work
    def run( self ):
        dest_crs = self.canvas.mapRenderer().destinationCrs()

        poly = self.get_selected_polygons()
        if not self.layer:
            if not poly:
                QMessageBox.critical( None, "Mask plugin error", "No polygon selection !" )
                return
            # or create a new layer
            self.layer = QgsVectorLayer("MultiPolygon?crs=%s" % dest_crs.authid(), "Mask", "memory")
        else:
            # else : set poly = geometry from mask layer
            if not poly:
                f = QgsFeature()
                f.setGeometry(self.parameters.geometry)
                poly = [(f,self.layer.crs())]
        
        dlg = MainDialog( self.layer, self.parameters )
        r = dlg.exec_()
        if r == 1:
            rect = self.canvas.extent()
            self.parameters.geometry, self.parameters.complement_geometry = self.compute_mask_geometries( poly, rect )
            print "computed geometries", self.parameters.geometry

            # add a layer (save on disk before if needed)
            if self.parameters.do_save_as:
                self.layer = self.save_layer( self.layer, self.parameters.file_path, self.parameters.file_format )
                self.is_memory_layer = False

#            save_geom = self.parameters.complement_geometry if self.parameters.mask_mode == 'mask' else self.parameters.geometry
#            self.update_layer( self.layer, save_geom )
            self.parameters.save_to_layer( self.layer )

            self.add_layer( self.layer )
            self.canvas.refresh()

    def on_add_layer( self, layer ):
        if layer.name() == 'Mask':
            # Part of the MemorySaverLayer hack
            # We cannot access the memory layer yet, since the MemorySaveLayer slot may be called
            # AFTER this one
            # So we remember we must load parameters from this layer on the next access to $mask_geometry
            self.must_reload_from_layer = layer

    def on_remove_mask( self, layer_id ):
        if self.disable_remove_mask_signal:
            return

        layer = self.registry.mapLayer( layer_id )
        if not layer:
            return
        if not layer.name() == 'Mask':
            return
        for name, layer in self.registry.mapLayers().iteritems():
            if has_mask_filter( layer ):
                # remove mask filter from layer, if any
                pal = QgsPalLayerSettings()
                pal.readFromLayer( layer )
                pal = remove_mask_filter( pal )
                pal.writeToLayer( layer )

        self.parameters.limited_layers = []
        self.layer = None
        # disable memorysavelayerhack
        self.must_reload_from_layer = None

    def get_selected_polygons( self ):
        "return array of (polygon_feature,crs) from current selection"
        geos = []
        layers = self.registry.mapLayers()
        for name, layer in layers.iteritems():
            if not layer.type() == QgsMapLayer.VectorLayer:
                continue
            if not layer.isValid():
                continue
            for feature in layer.selectedFeatures():
                if feature.geometry() and feature.geometry().type() == QGis.Polygon:
                    geos.append( (feature, layer.crs()) )
        return geos

    def get_final_geometry( self, geoms, dest_crs, do_simplify = False, simplify_tol = 0.0 ):
        geom = None
        for f,crs in geoms:
            g = f.geometry()
            if crs.authid() != dest_crs.authid():
                print "transform"
                xform = QgsCoordinateTransform( crs, dest_crs )
                g.transform( xform )

            if do_simplify:
                g = g.simplify( simplify_tol )
            if geom is None:
                geom = QgsGeometry(g)
            else:
                # do an union here
                geom = geom.combine( g )

        return geom

    def update_layer( self, layer, geometry ):
        # insert or replace into ...
        pr = layer.dataProvider()
        print "# features", pr.featureCount()
        if pr.featureCount() == 0:
            fet = QgsFeature()
            fet.setGeometry(geometry)
            pr.addFeatures([ fet ])
        else:
            pr.changeGeometryValues( { 1 : geometry } )
        layer.updateExtents()

    def add_layer( self, layer ):
        # add a layer to the registry, if not already there
        layers = self.registry.mapLayers()
        for name, alayer in layers.iteritems():
            if alayer == layer:
                return

        self.registry.addMapLayer(layer)
        self.iface.legendInterface().refreshLayerSymbology( layer ) 
#        self.registry.clearAllLayerCaches () #clean cache to allow mask layer to appear on refresh

    def copy_layer_style( self, layer, nlayer ):
        symbology = layer.rendererV2().clone()
        blend_mode = layer.blendMode()
        feature_blend_mode = layer.featureBlendMode()
        transparency = layer.layerTransparency()

        nlayer.setLayerTransparency( transparency )
        nlayer.setFeatureBlendMode( feature_blend_mode )
        nlayer.setBlendMode( blend_mode )
        nlayer.setRendererV2( symbology )

    def save_layer( self, layer, save_as, save_format ):
        error = QgsVectorFileWriter.writeAsVectorFormat( layer, save_as, "system", layer.crs(), save_format )
        if error == 0:
            nlayer = QgsVectorLayer( save_as, "Mask", "ogr" )
            self.copy_layer_style( layer, nlayer )

            self.disable_remove_mask_signal = True
            print "removing layer", layer.id()
            self.registry.removeMapLayer( layer.id() )
            self.disable_remove_mask_signal = False
            return nlayer
        return None

    def mask_geometry( self ):
        if self.must_reload_from_layer:
            # will force loading of parameters the first time the mask geometry is accessed
            # this will happen AFTER MemorySaveLayer has loaded memory layers
            self.layer = self.must_reload_from_layer
            self.parameters.load_from_layer( self.layer )
            self.must_reload_from_layer = None

        if not self.parameters.geometry:
            return QgsGeometry()
        return self.parameters.geometry

    def mask_complement_geometry( self ):
        if not self.parameters.complement_geometry:
            return QgsGeometry()
        return self.parameters.complement_geometry



