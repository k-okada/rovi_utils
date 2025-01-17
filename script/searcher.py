#!/usr/bin/python

import cv2
import numpy as np
import math
import roslib
import rospy
import tf
import tf2_ros
import open3d as o3d
import copy
import os
import sys
import yaml
from rovi.msg import Floats
from rospy.numpy_msg import numpy_msg
from std_msgs.msg import Bool
from std_msgs.msg import String
from std_msgs.msg import Float32MultiArray
from std_msgs.msg import MultiArrayDimension
from sensor_msgs.msg import PointCloud
from geometry_msgs.msg import Transform
from geometry_msgs.msg import TransformStamped
from rovi_utils import tflib
from scipy import optimize

Param={'radius_normal':2.0,'radius_feature':5.0,'maxnn_normal':30,'maxnn_feature':100,'distance_threshold':1.5,'icp_threshold':5.0}
Config={
  "path":"recipe",
  "scenes":["surface"],
  "solver":"o3d_solver",
  "scene_frame_id":["camera/capture0"],
  "master_frame_id":["camera/master0"],
  "place_frame_id":"camera/capture0",
  "tf_delay": 0.1
}

def P0():
  return np.array([]).reshape((-1,3))

def np2F(d):  #numpy to Floats
  f=Floats()
  f.data=np.ravel(d)
  return f

def cb_master(event):
  for n,l in enumerate(Config["scenes"]):
    if Model[n] is not None: pub_pcs[n].publish(np2F(Model[n]))

def cb_save(msg):
  global Model,tfReg
#save point cloud
  for n,l in enumerate(Config["scenes"]):
    if Scene[n] is None: continue
    pc=o3d.PointCloud()
    m=Scene[n]
    Model[n]=m
    pc.points=o3d.Vector3dVector(m)
    o3d.write_point_cloud(Config["path"]+"/"+l+".ply",pc,True,False)
    pub_pcs[n].publish(np2F(m))
  tfReg=[]
#copy TF scene...->master... and save them
  for n,s in enumerate(Config["scene_frame_id"]):
    try:
      tf=tfBuffer.lookup_transform("world",s,rospy.Time())
    except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
      pub_msg.publish("searcher::capture::lookup failure world->"+s)
      tf=TransformStamped()
      tf.header.stamp=rospy.Time.now()
      tf.header.frame_id="world"
      tf.child_frame_id=s
      tf.transform.rotation.w=1
    path=Config["path"]+"/"+Config["master_frame_id"][n].replace('/','_')+".yaml"
    f=open(path,"w")
    f.write(yaml.dump(tflib.tf2dict(tf.transform)))
    f.close()
    tfReg.append(tf)
  broadcaster.sendTransform(tfReg)
  solver.learn(Model,Param)
  pub_msg.publish("searcher::model learning completed")

def cb_load(msg):
  global Model,tfReg
#load point cloud
  for n,l in enumerate(Config["scenes"]):
    pcd=o3d.read_point_cloud(Config["path"]+"/"+l+".ply")
    Model[n]=np.reshape(np.asarray(pcd.points),(-1,3))
  rospy.Timer(rospy.Duration(Config["tf_delay"]),cb_master,oneshot=True)
  tfReg=[]
#load TF such as master/camera...
  for n,m in enumerate(Config["master_frame_id"]):
    path=Config["path"]+"/"+m.replace('/','_')+".yaml"
    try:
      f=open(path, "r+")
    except Exception:
      pub_msg.publish("searcher::file error "+path)
    else:
      yd=yaml.load(f)
      f.close()
      trf=tflib.dict2tf(yd)
      tf=TransformStamped()
      tf.header.stamp=rospy.Time.now()
      tf.header.frame_id="world"
      tf.child_frame_id=m
      tf.transform=trf
      print "world->",m
      print trf
      tfReg.append(tf)
  broadcaster.sendTransform(tfReg)
  Param.update(rospy.get_param("~param"))
  solver.learn(Model,Param)
  pub_msg.publish("searcher::model learning completed")

def cb_score(event):
  global solveResult
  ret=Bool();ret.data=True;pub_Y2.publish(ret)
  cb_master(event)
  score=Float32MultiArray()
  for sc in solveResult:
    d=MultiArrayDimension()
    d.label=sc
    d.size=len(solveResult[sc])
    d.stride=1
    print "score",d.label
#    score.dim.append(d)
#  score.data_offset=0

def cb_solve(msg):
  global solveResult,tfSolve
  if [x for x in Scene if x is None]:
    pub_msg.publish("searcher::short scene data")
    ret=Bool();ret.data=False;pub_Y2.publish(ret)
    return
  Param.update(rospy.get_param("~param"))
  solveResult=solver.solve(Scene,Param)
  RTs=solveResult["transform"]
  pub_msg.publish("searcher::"+str(len(RTs))+" model searched")
  tfSolve=[]
  for n,rt in enumerate(RTs):
    tf=TransformStamped()
    tf.header.stamp=rospy.Time.now()
    tf.header.frame_id=Config["place_frame_id"]
    tf.child_frame_id="solve"+str(n)
    tf.transform=tflib.fromRT(rt)
    tfSolve.append(tf)
  tfAll=copy.copy(tfReg)
  tfAll.extend(tfSolve)
  broadcaster.sendTransform(tfAll)
  solveResult.pop("transform")   #to make cb_score publish other member but for "transform"
  rospy.Timer(rospy.Duration(Config["tf_delay"]),cb_score,oneshot=True)

def cb_ps(msg,n):
  global Scene
  pc=np.reshape(msg.data,(-1,3))
  Scene[n]=pc
  print "cb_ps",pc.shape

def cb_tfreset(event):
  global tfSolve
  tfSolve=[]
  broadcaster.sendTransform(tfReg)

def cb_clear(msg):
  global Scene,tfSolve
  for n,l in enumerate(Config["scenes"]):
    Scene[n]=None
  tfAll=copy.copy(tfReg)
  for tf in tfSolve:
    tf.transform.translation.x=0
    tf.transform.translation.y=0
    tf.transform.translation.z=1000000
    tf.transform.rotation.x=0
    tf.transform.rotation.y=0
    tf.transform.rotation.z=0
    tf.transform.rotation.w=1
    tfAll.append(tf)
  broadcaster.sendTransform(tfAll)
  rospy.Timer(rospy.Duration(0.1),cb_tfreset,oneshot=True)
  rospy.Timer(rospy.Duration(0.2),cb_master,oneshot=True)

def parse_argv(argv):
  args={}
  for arg in argv:
    tokens = arg.split(":=")
    if len(tokens) == 2:
      key = tokens[0]
      args[key] = tokens[1]
  return args

########################################################

rospy.init_node("searcher",anonymous=True)
Config.update(parse_argv(sys.argv))
try:
  Config.update(rospy.get_param("~config"))
except Exception as e:
  print "get_param exception:",e.args
print "Config",Config
try:
  Param.update(rospy.get_param("~param"))
except Exception as e:
  print "get_param exception:",e.args
print "Param",Param

###load solver
exec("import "+Config["solver"]+" as solver")

###I/O
pub_pcs=[]
for n,c in enumerate(Config["scenes"]):
  rospy.Subscriber("~in/"+c+"/floats",numpy_msg(Floats),cb_ps,n)
  pub_pcs.append(rospy.Publisher("~master/"+c+"/floats",numpy_msg(Floats),queue_size=1))
pub_Y2=rospy.Publisher("~solved",Bool,queue_size=1)
pub_score=rospy.Publisher("~score",Float32MultiArray,queue_size=1)
rospy.Subscriber("~clear",Bool,cb_clear)
rospy.Subscriber("~solve",Bool,cb_solve)
rospy.Subscriber("~save",Bool,cb_save)
rospy.Subscriber("~load",Bool,cb_load)
pub_msg=rospy.Publisher("/message",String,queue_size=1)

###TF
tfBuffer=tf2_ros.Buffer()
listener=tf2_ros.TransformListener(tfBuffer)
broadcaster=tf2_ros.StaticTransformBroadcaster()

###data
Scene=[None]*len(Config["scenes"])
Model=[None]*len(Config["scenes"])
tfReg=[]
tfSolve=[]

try:
  rospy.spin()
except KeyboardInterrupt:
  print "Shutting down"
