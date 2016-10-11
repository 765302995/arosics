# -*- coding: utf-8 -*-
__author__='Daniel Scheffler'

import collections
import multiprocessing
import os
import warnings

# custom
import gdal
import numpy as np
from geopandas        import GeoDataFrame
from pykrige.ok       import OrdinaryKriging
from shapely.geometry import Point

# internal modules
from .CoReg  import COREG
from .       import geometry as GEO
from .       import io       as IO
from py_tools_ds.ptds                import GeoArray
from py_tools_ds.ptds.geo.projection import isProjectedOrGeographic, get_UTMzone



global_shared_imref    = None
global_shared_im2shift = None


class Geom_Quality_Grid(object):
    def __init__(self, im_ref, im_tgt, grid_res, window_size=(256,256), dir_out=None, projectName=None, multiproc=True,
                 r_b4match=1, s_b4match=1, max_iter=5, max_shift=5, data_corners_im0=None,
                 data_corners_im1=None, outFillVal=-9999, nodata=(None,None), calc_corners=True, binary_ws=True,
                 v=False, q=False):

        """

        :param im_ref(str, GeoArray):   source path of reference image (any GDAL compatible image format is supported)
        :param im_tgt(str, GeoArray):   source path of image to be shifted (any GDAL compatible image format is supported)
        :param grid_res:                grid resolution in pixels of the target image
        :param window_size(tuple):      custom matching window size [pixels] (default: (512,512))
        :param dir_out:
        :param projectName:
        :param multiproc:               enables multiprocessing during calculation of geometric quality grid (default: True)
        :param r_b4match(int):          band of reference image to be used for matching (starts with 1; default: 1)
        :param s_b4match(int):          band of shift image to be used for matching (starts with 1; default: 1)
        :param max_iter(int):           maximum number of iterations for matching (default: 5)
        :param max_shift(int):          maximum shift distance in reference image pixel units (default: 5 px)
        :param data_corners_im0(list):  map coordinates of data corners within reference image
        :param data_corners_im1(list):  map coordinates of data corners within image to be shifted
        :param outFillVal(int):         if given the generated geometric quality grid is filled with this value in case
                                        no match could be found during co-registration (default: -9999)
        :param nodata(tuple):           no data values for reference image and image to be shifted
        :param calc_corners(bool):      calculate true positions of the dataset corners in order to get a useful
                                        matching window position within the actual image overlap
                                        (default: 1; deactivated if '-cor0' and '-cor1' are given
        :param binary_ws(bool):         use binary X/Y dimensions for the matching window (default: 1)
        :param v(bool):                 verbose mode (default: 0)
        :param q(bool):                 quiet mode (default: 0)
        """
        self.imref         = im_ref if isinstance(im_ref, GeoArray) else GeoArray(im_ref)
        self.im2shift      = im_tgt if isinstance(im_tgt, GeoArray) else GeoArray(im_tgt)
        self.dir_out       = dir_out
        self.grid_res      = grid_res
        self.window_size   = window_size
        self.mp            = multiproc
        self.max_shift     = max_shift
        self.max_iter      = max_iter
        self.r_b4match     = r_b4match
        self.s_b4match     = s_b4match
        self.calc_corners  = calc_corners
        self.nodata        = nodata
        self.outFillVal    = outFillVal
        self.bin_ws        = binary_ws
        self.v             = v
        self.q             = q

        self.projectName   = projectName if projectName else 'UntitledProject_1'
        while projectName is None and os.path.isdir(os.path.join(self.dir_out,self.projectName)):
            self.projectName = '%s_%s' %(self.projectName.split('_')[0],int(self.projectName.split('_')[1])+1)
        self.dir_out = os.path.join(self.dir_out,self.projectName)
        if not os.path.exists(self.dir_out): os.makedirs(self.dir_out)

        gdal.AllRegister()

        self.COREG_obj = COREG(self.imref, self.im2shift,
                               data_corners_im0 = data_corners_im0,
                               data_corners_im1 = data_corners_im1,
                               calc_corners     = calc_corners,
                               r_b4match        = r_b4match,
                               s_b4match        = s_b4match,
                               max_iter         = max_iter,
                               max_shift        = max_shift,
                               nodata           = nodata,
                               multiproc        = multiproc,
                               binary_ws        = self.bin_ws,
                               v                = v,
                               q                = q,
                               ignore_errors    = True)

        self.ref_shape                = [self.COREG_obj.ref  .rows, self.COREG_obj.ref  .cols]
        self.tgt_shape                = [self.COREG_obj.shift.rows, self.COREG_obj.shift.cols]
        self.corner_coord_imref       = self.COREG_obj.ref  .corner_coord
        self.corner_coord_im2shift    = self.COREG_obj.shift.corner_coord
        self.overlap_poly             = self.COREG_obj.overlap_poly
        self.nodata                   = (self.COREG_obj.ref.nodata, self.COREG_obj.shift.nodata)

        self.XY_points, self.XY_mapPoints = self._get_imXY__mapXY_points(self.grid_res)
        self.quality_grid                 = None # set by self.get_quality_grid()


    def _get_imXY__mapXY_points(self,grid_res):
        Xarr,Yarr       = np.meshgrid(np.arange(0,self.tgt_shape[1],grid_res),
                                      np.arange(0,self.tgt_shape[0],grid_res))

        ULmapYX, URmapYX, LRmapYX, LLmapYX = self.im2shift.box.boxMapYX

        mapXarr,mapYarr = np.meshgrid(np.arange(ULmapYX[1],LRmapYX[1],     self.grid_res*self.COREG_obj.shift.xgsd),
                                      np.arange(ULmapYX[0],LRmapYX[0],-abs(self.grid_res*self.COREG_obj.shift.ygsd)))

        XY_points      = np.empty((Xarr.size,2),Xarr.dtype)
        XY_points[:,0] = Xarr.flat
        XY_points[:,1] = Yarr.flat

        XY_mapPoints      = np.empty((mapXarr.size,2),mapXarr.dtype)
        XY_mapPoints[:,0] = mapXarr.flat
        XY_mapPoints[:,1] = mapYarr.flat

        return XY_points,XY_mapPoints


    @staticmethod
    def _get_spatial_shifts(coreg_kwargs):
        pointID = coreg_kwargs['pointID']
        del coreg_kwargs['pointID']

        CR = COREG(global_shared_imref, global_shared_im2shift, **coreg_kwargs)
        CR.calculate_spatial_shifts()
        res = pointID, CR.ref.win.size_YX[0], CR.x_shift_px, CR.y_shift_px
        return res


    def get_quality_grid(self,exclude_outliers=1,dump_values=1):
        assert self.XY_points is not None and self.XY_mapPoints is not None

        #ref_ds,tgt_ds = gdal.Open(self.path_imref),gdal.Open(self.path_im2shift)
        #ref_pathTmp, tgt_pathTmp = None,None
        #if ref_ds.GetDriver().ShortName!='ENVI':
        #    ref_pathTmp = IO.get_tempfile(ext='.bsq')
        #    IO.convert_gdal_to_bsq__mp(self.path_imref,ref_pathTmp)
        #    self.path_imref = ref_pathTmp
        #if tgt_ds.GetDriver().ShortName!='ENVI':
        #    tgt_pathTmp = IO.get_tempfile(ext='.bsq')
        #    IO.convert_gdal_to_bsq__mp(self.path_im2shift,tgt_pathTmp)
        #    self.path_im2shift = tgt_pathTmp
        #ref_ds=tgt_ds=None

        XYarr2PointGeom = np.vectorize(lambda X,Y: Point(X,Y), otypes=[Point])
        geomPoints      = np.array(XYarr2PointGeom(self.XY_mapPoints[:,0],self.XY_mapPoints[:,1]))

        if isProjectedOrGeographic(self.COREG_obj.shift.prj)=='geographic':
            crs = dict(ellps='WGS84', datum='WGS84', proj='longlat')
        elif isProjectedOrGeographic(self.COREG_obj.shift.prj)=='projected':
            UTMzone = abs(get_UTMzone(prj=self.COREG_obj.shift.prj))
            south   = get_UTMzone(prj=self.COREG_obj.shift.prj)<0
            crs     = dict(ellps='WGS84', datum='WGS84', proj='utm', zone=UTMzone,south=south,units='m', no_defs=True)
            if not south: del crs['south']
        else:
            crs = None

        GDF        = GeoDataFrame(index=range(len(geomPoints)),crs=crs,
                                  columns=['geometry','POINT_ID','X_IM','Y_IM','X_UTM','Y_UTM','WIN_SIZE',
                                           'X_SHIFT_PX','Y_SHIFT_PX','X_SHIFT_M','Y_SHIFT_M','ABS_SHIFT','ANGLE'])
        GDF       ['geometry']       = geomPoints
        GDF       ['POINT_ID']       = range(len(geomPoints))
        GDF.loc[:,['X_IM','Y_IM']]   = self.XY_points
        GDF.loc[:,['X_UTM','Y_UTM']] = self.XY_mapPoints
        GDF = GDF if not exclude_outliers else GDF[GDF['geometry'].within(self.overlap_poly)]
        GDF.loc[:,['WIN_SIZE','X_SHIFT_PX','Y_SHIFT_PX','X_SHIFT_M','Y_SHIFT_M','ABS_SHIFT','ANGLE']] = self.outFillVal # Fehlwert

        # declare global variables needed for self._get_spatial_shifts()
        global global_shared_imref,global_shared_im2shift
        global_shared_imref = \
            GeoArray(self.imref[self.r_b4match-1], self.imref.geotransform, self.imref.projection)
        global_shared_im2shift = \
            GeoArray(self.im2shift[self.s_b4match-1], self.im2shift.geotransform,self.im2shift.projection)

        # get all variations of kwargs for coregistration
        get_coreg_kwargs = lambda pID, wp: {
            'pointID'         : pID,
            'wp'              : wp,
            'ws'              : self.window_size,
            'data_corners_im0': self.corner_coord_imref,
            'data_corners_im1': self.corner_coord_im2shift,
            'r_b4match'       : self.r_b4match,
            's_b4match'       : self.s_b4match,
            'max_iter'        : self.max_iter,
            'max_shift'       : self.max_shift,
            'nodata'          : self.nodata,
            'binary_ws'       : self.bin_ws,
            'v'               : self.v,
            'q'               : self.q,
            'ignore_errors'   : True
        }
        list_coreg_kwargs = (get_coreg_kwargs(i, self.XY_mapPoints[i]) for i in GDF.index) # generator

        # run co-registration for whole grid
        if self.mp:
            print('multiprocessing')
            with multiprocessing.Pool() as pool:
                results = pool.map(self._get_spatial_shifts, list_coreg_kwargs)
        else:
            print('singleprocessing')
            results = []
            for i,coreg_kwargs in enumerate(list_coreg_kwargs):
                #print(argset[1])
                #if not 0<i<10: continue
                #if i>300 or i<100: continue
                #if i!=127: continue
                if i%100==0: print('Point #%s, ID %s' %(i,coreg_kwargs['pointID']))
                res = self._get_spatial_shifts(coreg_kwargs)
                results.append(res)

        for res in results:
            pointID                       = res[0]
            GDF.loc[pointID,'WIN_SIZE']   = res[1] if res[1] is not None else self.outFillVal
            GDF.loc[pointID,'X_SHIFT_PX'] = res[2] if res[2] is not None else self.outFillVal
            GDF.loc[pointID,'Y_SHIFT_PX'] = res[3] if res[3] is not None else self.outFillVal

        oFV = self.outFillVal
        GDF['X_SHIFT_M'] = [*GDF['X_SHIFT_PX'].map(lambda px: oFV if px==oFV else px*self.im2shift.xgsd)]
        GDF['Y_SHIFT_M'] = [*GDF['Y_SHIFT_PX'].map(lambda px: oFV if px==oFV else px*self.im2shift.ygsd)]
        get_absShift     = lambda row: float(np.sqrt(row['X_SHIFT_M']**2 + row['Y_SHIFT_M']**2))
        GDF['ABS_SHIFT'] = GDF.apply(lambda row: oFV if row['X_SHIFT_M']  == oFV else get_absShift(row), axis=1)
        get_angle        = lambda row: GEO.angle_to_north((row['X_SHIFT_PX'],row['Y_SHIFT_PX'])).tolist()[0]
        GDF['ANGLE']     = GDF.apply(lambda row: oFV if row['X_SHIFT_PX'] == oFV else get_angle(row)   , axis=1)


        if dump_values:
            fName_out = "CoRegMatrix_grid%s_ws%s__T_%s__R_%s.pkl" %(self.grid_res, self.window_size, os.path.splitext(
                os.path.basename(self.im2shift.filePath))[0], os.path.splitext(os.path.basename(self.imref.filePath))[0]) # FIXME does not work for inmem GeoArrays
            path_out  = os.path.join(self.dir_out, 'CoRegMatrix', fName_out)
            if not os.path.exists(os.path.dirname(path_out)): os.makedirs(os.path.dirname(path_out))
            GDF.to_pickle(path_out)

        if dump_values:
            self.dump_quality_grid()

        return GDF


    def dump_quality_grid(self):
        assert self.quality_grid is not None, 'Quality is None. Thus dumping it to disk makes no sense.'

        fName_out = "CoRegMatrix_grid%s_ws%s__T_%s__R_%s.pkl" % (self.grid_res, self.window_size, os.path.splitext(
            os.path.basename(self.im2shift.filePath))[0], os.path.splitext(os.path.basename(self.imref.filePath))[
                                                                     0])  # FIXME does not work for inmem GeoArrays
        path_out = os.path.join(self.dir_out, 'CoRegMatrix', fName_out)
        if not os.path.exists(os.path.dirname(path_out)): os.makedirs(os.path.dirname(path_out))
        self.quality_grid.to_pickle(path_out)


    def test_if_singleprocessing_equals_multiprocessing_result(self):
        self.mp = 1
        dataframe = self.get_quality_grid()
        mp_out    = np.empty_like(dataframe.values)
        mp_out[:] = dataframe.values
        self.mp = 0
        dataframe = self.get_quality_grid()
        sp_out    = np.empty_like(dataframe.values)
        sp_out[:] = dataframe.values

        return np.array_equal(sp_out,mp_out)


    def get_line_by_PID(self,PID):
        assert self.quality_grid, 'Calculate quality grid first!'
        return self.quality_grid.loc[PID,:]


    def get_lines_by_PIDs(self,PIDs):
        assert self.quality_grid, 'Calculate quality grid first!'
        assert isinstance(PIDs,list)
        lines = np.zeros((len(PIDs),self.quality_grid.shape[1]))
        for i,PID in enumerate(PIDs):
            lines[i,:] = self.quality_grid[self.quality_grid['POINT_ID']==PID]
        return lines


    def quality_grid_to_PointShapefile(self,skip_nodata=1,skip_nodata_col = 'ABS_SHIFT'):
        GDF            = self.quality_grid
        GDF2pass       = GDF if not skip_nodata else GDF[GDF[skip_nodata_col]!=self.outFillVal]

        fName_out = "CoRegPoints_grid%s_ws(%s_%s)__T_%s__R_%s.shp" \
                    %(self.grid_res, self.window_size[0], self.window_size[1],os.path.splitext(
            os.path.basename(self.im2shift.filePath))[0], os.path.splitext(os.path.basename(self.imref.filePath))[0]) # FIXME does not work for inmem GeoArrays
        path_out  = os.path.join(self.dir_out, 'CoRegPoints', fName_out)
        if not os.path.exists(os.path.dirname(path_out)): os.makedirs(os.path.dirname(path_out))
        print('Writing %s ...' %path_out)
        GDF2pass.to_file(path_out)


    def _quality_grid_to_PointShapefile(self,skip_nodata=1,skip_nodata_col = 'ABS_SHIFT'):
        warnings.warn(DeprecationWarning("'_quality_grid_to_PointShapefile' deprecated."
                                         " 'quality_grid_to_PointShapefile' is much faster."))
        GDF            = self.quality_grid
        GDF2pass       = GDF if not skip_nodata else GDF[GDF[skip_nodata_col]!=self.outFillVal]
        shapely_points = GDF2pass['geometry'].values.tolist()
        attr_dicts     = [collections.OrderedDict(zip(GDF2pass.columns,GDF2pass.loc[i].values)) for i in GDF2pass.index]


        fName_out = "CoRegPoints_grid%s_ws%s.shp" %(self.grid_res, self.window_size)
        path_out = os.path.join(self.dir_out, fName_out)
        IO.write_shp(path_out, shapely_points, prj=self.COREG_obj.shift.prj, attrDict=attr_dicts)


    def quality_grid_to_Raster_using_KrigingOLD(self,attrName,skip_nodata=1,skip_nodata_col='ABS_SHIFT',outGridRes=None,
                                             fName_out=None,tilepos=None):
        GDF             = self.quality_grid
        GDF2pass        = GDF if not skip_nodata else GDF[GDF[skip_nodata_col]!=self.outFillVal]

        # subset if tilepos is given
        rows,cols = tilepos if tilepos else self.tgt_shape
        GDF2pass        = GDF2pass.loc[(GDF2pass['X_IM']>=cols[0])&(GDF2pass['X_IM']<=cols[1])&
                                       (GDF2pass['Y_IM']>=rows[0])&(GDF2pass['Y_IM']<=rows[1])]


        X_coords,Y_coords,ABS_SHIFT = GDF2pass['X_UTM'], GDF2pass['Y_UTM'],GDF2pass[attrName]

        xmin,ymin,xmax,ymax = GDF2pass.total_bounds

        grid_res            = outGridRes if outGridRes else int(min(xmax-xmin,ymax-ymin)/250)
        grid_x,grid_y       = np.arange(xmin, xmax+grid_res, grid_res), np.arange(ymax, ymin-grid_res, -grid_res)

        # Reference: P.K. Kitanidis, Introduction to Geostatistcs: Applications in Hydrogeology,
        #            (Cambridge University Press, 1997) 272 p.
        OK = OrdinaryKriging(X_coords, Y_coords, ABS_SHIFT, variogram_model='spherical',verbose=False)
        zvalues, sigmasq = OK.execute('grid', grid_x, grid_y)#,backend='C',)

        fName_out = fName_out if fName_out else \
            "Kriging__%s__grid%s_ws%s.tif" %(attrName,self.grid_res, self.window_size)
        path_out  = os.path.join(self.dir_out, fName_out)
        print('Writing %s ...' %path_out)
        # add a half pixel grid points are centered on the output pixels
        xmin,ymin,xmax,ymax = xmin-grid_res/2,ymin-grid_res/2,xmax+grid_res/2,ymax+grid_res/2
        IO.write_numpy_to_image(zvalues, path_out, gt=(xmin, grid_res, 0, ymax, 0, -grid_res), prj=self.COREG_obj.shift.prj)

        return zvalues


    def quality_grid_to_Raster_using_Kriging(self,attrName,skip_nodata=1,skip_nodata_col='ABS_SHIFT',outGridRes=None,
                                             fName_out=None,tilepos=None,tilesize=500,mp=None):

        mp = mp if mp else self.mp
        self.Kriging_sp(attrName,skip_nodata=skip_nodata,skip_nodata_col=skip_nodata_col,
                outGridRes=outGridRes,fName_out=fName_out,tilepos=tilepos)

        # if mp:
        #     tilepositions = UTL.get_image_tileborders([tilesize,tilesize],self.tgt_shape)
        #     args_kwargs_dicts=[]
        #     for tp in tilepositions:
        #         kwargs_dict = {'skip_nodata':skip_nodata,'skip_nodata_col':skip_nodata_col,'outGridRes':outGridRes,
        #                        'fName_out':fName_out,'tilepos':tp}
        #         args_kwargs_dicts.append({'args':[attrName],'kwargs':kwargs_dict})
        #     # self.kriged=[]
        #     # for i in args_kwargs_dicts:
        #     #     res = self.Kriging_mp(i)
        #     #     self.kriged.append(res)
        #     #     print(res)
        #
        #     with multiprocessing.Pool() as pool:
        #        self.kriged = pool.map(self.Kriging_mp,args_kwargs_dicts)
        # else:
        #     self.Kriging_sp(attrName,skip_nodata=skip_nodata,skip_nodata_col=skip_nodata_col,
        #                     outGridRes=outGridRes,fName_out=fName_out,tilepos=tilepos)
        res = self.kriged if mp else None
        return res


    def Kriging_sp(self,attrName,skip_nodata=1,skip_nodata_col='ABS_SHIFT',outGridRes=None,
                                             fName_out=None,tilepos=None):
        GDF             = self.quality_grid
        GDF2pass        = GDF if not skip_nodata else GDF[GDF[skip_nodata_col]!=self.outFillVal]

#         # subset if tilepos is given
# #        overlap_factor =
#         rows,cols = tilepos if tilepos else self.tgt_shape
#         xvals, yvals = np.sort(GDF2pass['X_IM'].values.flat),np.sort(GDF2pass['Y_IM'].values.flat)
#         cS,cE = UTL.find_nearest(xvals,cols[0],'off',1), UTL.find_nearest(xvals,cols[1],'on',1)
#         rS,rE = UTL.find_nearest(yvals,rows[0],'off',1), UTL.find_nearest(yvals,rows[1],'on',1)
#         # GDF2pass        = GDF2pass.loc[(GDF2pass['X_IM']>=cols[0])&(GDF2pass['X_IM']<=cols[1])&
#         #                                (GDF2pass['Y_IM']>=rows[0])&(GDF2pass['Y_IM']<=rows[1])]
#         GDF2pass        = GDF2pass.loc[(GDF2pass['X_IM']>=cS)&(GDF2pass['X_IM']<=cE)&
#                                        (GDF2pass['Y_IM']>=rS)&(GDF2pass['Y_IM']<=rE)]

        X_coords,Y_coords,ABS_SHIFT = GDF2pass['X_UTM'], GDF2pass['Y_UTM'],GDF2pass[attrName]

        xmin,ymin,xmax,ymax = GDF2pass.total_bounds

        grid_res            = outGridRes if outGridRes else int(min(xmax-xmin,ymax-ymin)/250)
        grid_x,grid_y       = np.arange(xmin, xmax+grid_res, grid_res), np.arange(ymax, ymin-grid_res, -grid_res)

        # Reference: P.K. Kitanidis, Introduction to Geostatistcs: Applications in Hydrogeology,
        #            (Cambridge University Press, 1997) 272 p.
        OK = OrdinaryKriging(X_coords, Y_coords, ABS_SHIFT, variogram_model='spherical',verbose=False)
        zvalues, sigmasq = OK.execute('grid', grid_x, grid_y,backend='C',n_closest_points=12)

        if self.mp:
            fName_out = fName_out if fName_out else \
                "Kriging__%s__grid%s_ws%s_%s.tif" %(attrName,self.grid_res, self.window_size,tilepos)
        else:
            fName_out = fName_out if fName_out else \
                "Kriging__%s__grid%s_ws%s.tif" %(attrName,self.grid_res, self.window_size)
        path_out  = os.path.join(self.dir_out, fName_out)
        print('Writing %s ...' %path_out)
        # add a half pixel grid points are centered on the output pixels
        xmin,ymin,xmax,ymax = xmin-grid_res/2,ymin-grid_res/2,xmax+grid_res/2,ymax+grid_res/2
        IO.write_numpy_to_image(zvalues, path_out, gt=(xmin, grid_res, 0, ymax, 0, -grid_res), prj=self.COREG_obj.shift.prj)

        return zvalues


    def Kriging_mp(self,args_kwargs_dict):
        args   = args_kwargs_dict.get('args',[])
        kwargs = args_kwargs_dict.get('kwargs',[])

        return self.Kriging_sp(*args,**kwargs)